"""Лиды для расширения «Автозаявки»: отдать и принять результат.

ЧТО ЭТО. У владельца есть расширение Chrome, которое заводит заявки в трёх
лид-центрах (БТ / КП / МНЧ). Связь только исходящая — офисная машина за NAT, —
поэтому расширение само ходит сюда по расписанию. Наша сторона обязана отдавать
лиды и запоминать, чем кончилось.

ЧТО СЧИТАЕТСЯ ЛИДОМ (решение владельца от 13 августа):

  диалог, которому ЧЕЛОВЕК поставил итог «Выезд», и у клиента известен телефон.

Не «любой диалог с телефоном»: тогда в лид-центр поехали бы заявки на тех, кто
спросил цену и пропал, а каждая лишняя заявка — это возможный выезд мастера
впустую. Итог «Выезд» ставит диспетчер руками, то есть за каждой заявкой стоит
решение человека.

⚠ ГЛАВНОЕ ПРАВИЛО ЭТОГО ФАЙЛА: ЛИД, КОТОРЫЙ НЕЛЬЗЯ ОТДАТЬ, НЕ ПРОПАДАЕТ МОЛЧА.

На боевом сервере из шести диалогов с итогом «Выезд» телефон в карточке есть у
ДВУХ. Тихо пропустить остальные четыре значило бы потерять две трети заявок так,
что никто никогда об этом не узнает: расширение исправно опрашивает, LeadChat
исправно отвечает пустым списком, все довольны. Поэтому непригодные лиды
собираются отдельно (`held_back`) вместе с причиной и показываются человеку.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.avito.listing_url import parse_listing_url
from app.models.account import AvitoAccount
from app.models.client import CANDIDATE_ACCEPTED, Client, ClientPhoneCandidate
from app.models.conversation import Conversation
from app.models.lead import DECISIONS, LeadHandout
from app.models.message import Message
from app.services import lead_comment, lead_direction, lead_flags
from app.services.avito_cities import city_by_slug

log = structlog.get_logger("app.leads")

#: Итог диалога, который означает «стало заявкой». Один из пяти в
#: `conversations.outcome`; остальные (отказ, не наш профиль, спам, без ответа)
#: заявкой не становятся по определению.
OUTCOME_VISIT = "visit"

#: Закрытый словарь итогов диалога — тот же список, что в CHECK-ограничении
#: `ck_conversations_outcome` (модель это оговаривает: словарь двигают правкой
#: кода и миграцией, а не на лету). Здесь он нужен шагу бота `close`: итог из
#: сценария обязан сверяться со словарём ДО записи, иначе опечатка в сценарии
#: превращалась бы в ошибку базы на живом диалоге.
OUTCOMES: tuple[str, ...] = ("visit", "declined", "not_our_profile", "spam", "no_reply")

#: Сколько ждать подтверждения, прежде чем отдать лид снова.
#:
#: Расширение подтверждает сразу после создания заявки, и обычно `acked_at`
#: заполняется в ту же минуту. Но подтверждение может не дойти — оно уходит
#: «глотая ошибку», чтобы недоступность LeadChat не мешала заводить заявки.
#: Тогда лид останется неподтверждённым навсегда, и мы про него забудем.
#:
#: Час — заведомо больше любой задержки и заведомо меньше смены. Повторная
#: выдача дублем не грозит: расширение помнит виденные `uid` и, главное, само
#: проверяет телефон в лид-центре перед созданием.
RETRY_UNACKED_AFTER = timedelta(hours=1)

#: Сколько попыток выдачи делаем, прежде чем перестать (аудит 19.08, L-005).
#: Сутки почасовых повторов: если подтверждения нет столько, дело не в сети —
#: расширение не работает, ключ не тот или лид-центр не принимает. Повтор этого
#: не чинит, а поток одинаковых строк мешает увидеть настоящие лиды.
RETRY_GIVE_UP_AFTER_ATTEMPTS = 24

#: Заявки старше этого в выборку не попадают: догонять прожитое нечем.
LEAD_MAX_AGE_DAYS = 14

#: Потолок одного ответа: расширение забирает лиды порциями, а не всю историю.
LEAD_PAGE_LIMIT = 200

#: Сколько символов последней реплики клиента кладём в комментарий заявки.
#: Мастеру нужно «что случилось», а не переписка целиком.
COMMENT_LIMIT = 500


@dataclass(frozen=True)
class HeldBack:
    """Диалог, который заявкой стать не может, и почему."""

    conversation_id: uuid.UUID
    client_name: str | None
    account_title: str
    reason: str

    #: Причины. Каждая — своя починка, и общее «не получилось» их бы склеило.
    NO_PHONE = "нет телефона клиента"
    #: ⚠ Текст называет ВСЕ несработавшие пути. С 15.08 направление сперва
    #: читается из слов клиента (`lead_direction.detect_from_problem`), затем из
    #: объявления (`detect`), и придержка случается только когда не сработали
    #: ВСЕ ТРИ: в репликах нет слов техники, объявление неизвестно или ни на
    #: что не похоже, И запасного выбора у канала нет.
    NO_SRC = (
        "направление не определилось ни по словам клиента, ни по объявлению, "
        "а запасной лид-центр у канала не выбран"
    )
    NO_CITY = "город не распознан"
    #: Регламент 15.08, п. 3: белые заявки (партнёр 723) на БТ не создаются —
    #: только КП и МНЧ. Заявка не теряется: висит здесь, человек решает.
    WHITE_BT = "белая заявка (партнёр 723) на БТ не создаётся — только КП и МНЧ"
    #: Карточка объединена, а номер не привязан ни к одному диалогу — какой из
    #: собранных под ней людей его назвал, неизвестно. Отправить наугад значит
    #: послать мастера к постороннему.
    PHONE_AMBIGUOUS = "карточка объединена, а телефон не привязан к этому диалогу"


async def lead_phone(db: AsyncSession, conv: Conversation, client: Client) -> str | None:
    """Телефон, который можно отдать в заявку ПО ЭТОМУ диалогу.

    ⚠ ЗАЧЕМ ЭТО ВООБЩЕ. Заявка собиралась из карточки — `client.phone`. Пока карточка
    описывает одного человека, это верно. Но карточки объединяются, и после
    объединения в `phone` может лежать номер, названный в СОСЕДНЕМ диалоге: заявка
    уходит в лид-центр, мастер едет к другому человеку и заводит разговор о чужом
    ремонте.

    Это худшее последствие ошибочной склейки. Показ лишней истории на экране
    неприятен и обратим; выезд — нет. Поэтому граница закрывается ЗДЕСЬ, отдельно от
    самой связки и раньше неё: даже если объединение ошиблось, наружу уйдёт либо
    верный номер, либо ничего.

    ЧЕТЫРЕ ПУТИ, В ПОРЯДКЕ УБЫВАНИЯ ДОКАЗАННОСТИ:

    1. принятый кандидат ЭТОГО диалога — номер написан этим человеком в этой
       переписке и подтверждён оператором. Сильнее не бывает;
    2. `client.phone`, привязанный к этому диалогу (`phone_conversation_id`) —
       оператор ввёл его, находясь здесь;
    3. `client.phone` у карточки, которая НИКОГДА не объединялась — путать нечего:
       все диалоги под ней её собственные. Этот путь держит совместимость: у всех
       строк до миграции 0044 происхождение неизвестно, и без него каждая
       сегодняшняя заявка встала бы;
    3′. карточка объединена, но у ВСЕХ присоединённых тот же номер (правка
       12.09 под объединение двойников по телефону): все люди под карточкой
       назвали один номер — чей бы ни был диалог, номер его;
    4. иначе — ``None``. Карточка объединена, происхождение номера неизвестно, и
       угадывать мы не будем.

    ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО: номера, записанного ботом (`app/bots/engine.py`). Он
    приходит без диалога и без подтверждения человеком; путь 3 его пропустит у
    необъединённой карточки — там он безопасен, — а у объединённой не пропустит.
    """
    phone = (client.phone or "").strip()

    # 1. Кандидат этой переписки, принятый человеком, — И СТАВШИЙ ОСНОВНЫМ.
    #
    # ⚠ «ПРИНЯТ» И «ОСНОВНОЙ» — РАЗНЫЕ ВЕЩИ (28.08). `CANDIDATE_ACCEPTED` ставят
    # ОБЕ кнопки: и «Заменить», и «Добавить». А «Добавить» по собственному
    # докстрингу `resolve_phone_candidate` означает «телефон его, но ОСНОВНОЙ
    # ДРУГОЙ»: так помечают второй номер — «звоните жене». Карточка показывает
    # его отдельно и с пометкой `"primary": False`, `clients.phone` при этом не
    # трогается вовсе.
    #
    # Здесь же принятый кандидат возвращался первым и без оговорок, то есть
    # заявка уезжала в лид-центр с номером жены вместо номера клиента. Ошибка
    # тихая до самого звонка: в карточке номер правильный, в заявке — другой.
    #
    # Спрашиваем то, что имели в виду: номер, который стал ОСНОВНЫМ, и чьё
    # происхождение доказано этой перепиской. «Заменить» ставит и `client.phone`,
    # и `phone_conversation_id` (см. `resolve_phone_candidate`), поэтому сравнение
    # с основным — точный признак, а не догадка. Не совпал — идём ниже, к самому
    # `client.phone`: там правда свежее.
    if not phone:
        return None
    # ⚠ «ЕСТЬ ЛИ принятая строка с ЭТИМ номером в ЭТОМ диалоге», а не «последняя
    # принятая равна номеру» (12.09): дополнительные номера теперь ложатся
    # принятыми строками сами, и последняя из них — не обязательно основной.
    accepted = (
        await db.execute(
            select(ClientPhoneCandidate.id)
            .where(
                ClientPhoneCandidate.conversation_id == conv.id,
                ClientPhoneCandidate.status == CANDIDATE_ACCEPTED,
                ClientPhoneCandidate.phone == phone,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if accepted is not None:
        return phone

    # 2. Номер карточки, доказанный этим же диалогом.
    if client.phone_conversation_id == conv.id:
        return phone

    # 3. Карточка ни разу не объединялась — перепутать не с кем.
    merged_in = (
        await db.execute(
            select(sa.func.count()).select_from(Client).where(Client.merged_into_id == client.id)
        )
    ).scalar_one()
    if not merged_in:
        return phone

    # 3′. Объединена, но все присоединённые назвали ТОТ ЖЕ номер (12.09,
    # двойники по телефону) — чей бы ни был диалог, номер его. Сравниваются
    # живые поля: впишут победителю другой номер руками — путь молчит.
    другие_номера = (
        await db.execute(
            select(sa.func.count())
            .select_from(Client)
            .where(Client.merged_into_id == client.id, Client.phone.is_distinct_from(phone))
        )
    ).scalar_one()
    if not другие_номера:
        return phone

    # 4. Объединена, происхождение неизвестно — молчим.
    return None


async def _visit_conversations(
    db: AsyncSession,
) -> list[tuple[Conversation, Client, AvitoAccount]]:
    """Диалоги с итогом «Выезд» — все, годные и нет."""
    порог_возраста = datetime.now(UTC) - timedelta(days=LEAD_MAX_AGE_DAYS)
    rows = await db.execute(
        select(Conversation, Client, AvitoAccount)
        .join(Client, Client.id == Conversation.client_id)
        .join(AvitoAccount, AvitoAccount.id == Conversation.account_id)
        .where(
            Conversation.outcome == OUTCOME_VISIT,
            AvitoAccount.is_service.is_(False),
            # ⚠ ГРАНИЦА ПО ВРЕМЕНИ (аудит 19.08, находка L-005). Выборка не имела
            # НИ ОДНОЙ границы и ни лимита: первый же опрос после выпуска токена
            # отдал бы весь исторический хвост целиком — каждую старую заявку
            # заново, то есть мастера, выехавшего дважды. При 86 диалогах это
            # незаметно, при 3000 в сутки — тысячи строк в одном ответе.
            #
            # Две недели выбраны по делу: заявка старше двух недель уже прожита
            # лид-центром, и «догонять» её нечем. Диалоги без отметки итога
            # (outcome_at пуст) сюда не попадают — у них нечего просрочивать.
            # Порог считаем в Python, а не средствами базы: `func.now()` живёт
            # по часам СУБД и на SQLite (тестовый стек) арифметику с timedelta
            # не делает вовсе — граница молча переставала работать, а тест
            # ловил это только потому, что был написан.
            Conversation.outcome_at.is_not(None),
            Conversation.outcome_at >= порог_возраста,
        )
        .order_by(Conversation.outcome_at.desc().nullslast())
        .limit(LEAD_PAGE_LIMIT)
    )
    return [(c, cl, a) for c, cl, a in rows.all()]


async def _already_handed(
    db: AsyncSession, now: datetime, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LeadHandout]:
    """Диалоги, которые уже отдавали. Ключ — диалог.

    ⚠ СТРОКИ БЕРУТСЯ ПОД ЗАМОК (аудит 19.08, находка L-006). Первичная выдача
    прикрыта уникальным ограничением по диалогу, а ветка ПОВТОРНОЙ выдачи не
    прикрыта ничем: два одновременных опроса (две вкладки расширения, перезапуск
    по таймеру) читали одну и ту же неподтверждённую строку и оба решали «пора
    отдать заново» — один лид уезжал в CRM дважды.

    `with_for_update` держит строки до конца транзакции ручки, поэтому второй
    опрос ждёт первого и видит уже обновлённый `handed_at`.

    ⚠ ЗАМОК БЕРЁМ ТОЛЬКО НА СТРОКИ ЭТОЙ ПОРЦИИ. Здесь стоял
    `select(LeadHandout).with_for_update()` без единого условия — то есть замок
    на ВСЮ таблицу выдач, а она не чистится: строка остаётся на каждую
    когда-либо отданную заявку. Держался он до конца транзакции ручки, а внутри
    неё идёт весь `collect`: до двухсот диалогов, и на каждый — телефон, тексты
    переписки и описание поломки, то есть запросы по партиционированной
    `messages`. Всё это время подтверждение заявок (`POST /leads/{uid}/ack`)
    ждало на том же замке.

    Список пуст — не блокируем ничего: пустой `IN ()` вернул бы нулевую выборку,
    но замок всё равно стоит брать осмысленно, а не «на всякий случай».
    """
    if not conversation_ids:
        return {}
    rows = await db.execute(
        select(LeadHandout)
        .where(LeadHandout.conversation_id.in_(list(conversation_ids)))
        .with_for_update()
    )
    return {row.conversation_id: row for row in rows.scalars().all()}


def _aware(moment: datetime) -> datetime:
    """Время с часовым поясом.

    Колонка на боевом — `timestamptz`, и оттуда приходит время с зоной. Но на
    SQLite (тестовый стек) зона теряется, и вычитание падает с «can't subtract
    offset-naive and offset-aware datetimes» — то есть выдача лидов ломается
    целиком. Поймано проверками; чинится приведением, а не запретом на SQLite.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _needs_handout(previous: LeadHandout | None, now: datetime) -> bool:
    """Отдавать ли лид сейчас.

    Отдаём: не отдавали вовсе; или отдали, подтверждения нет и час прошёл.
    НЕ отдаём: подтверждение получено — чем бы ни кончилось. Даже «лид-центр
    отклонил» повторять нельзя: причина отказа не рассосётся сама, а повтор
    каждые пять минут превратится в поток одинаковых ошибок.
    """
    if previous is None:
        return True
    if previous.acked_at is not None:
        return False
    # ⚠ ПОТОЛОК ПОВТОРОВ (аудит 19.08, находка L-005). Счётчика не было вовсе:
    # неподтверждённый лид отдавался заново КАЖДЫЙ ЧАС бесконечно, а `handed_at`
    # перезаписывался — история попыток стиралась, и по журналу это выглядело
    # как одна выдача. Если подтверждение не пришло за сутки попыток, дело не в
    # сети: расширение не работает, ключ не тот, лид-центр не принимает. Повтор
    # это не чинит, а шум мешает увидеть настоящие лиды.
    if previous.attempts >= RETRY_GIVE_UP_AFTER_ATTEMPTS:
        return False
    return _aware(now) - _aware(previous.handed_at) >= RETRY_UNACKED_AFTER


async def _dialog_texts(
    db: AsyncSession, conversation_id: uuid.UUID
) -> tuple[list[str], list[str]]:
    """Реплики клиента и наши — в порядке разговора.

    Нужны обе стороны и обе по-своему: из НАШИХ собирается, что мы клиенту
    пообещали (цена выезда, «диагностика бесплатно») — это защита мастера на
    пороге; из КЛИЕНТСКИХ — просил ли он созвониться. Галочки прозвона ставятся
    только по его словам: правило владельца прямо запрещает предлагать звонок
    самим (`app/services/lead_flags.py`).
    """
    rows = await db.execute(
        select(Message.direction, Message.body)
        .where(
            Message.conversation_id == conversation_id,
            Message.body.is_not(None),
            # Заметки — внутренняя переписка операторов, клиент их не видит.
            # Обещанием клиенту они не являются и в комментарий заявки не идут.
            Message.direction.in_(("in", "out")),
        )
        .order_by(Message.created_at)
        .limit(200)
    )
    incoming: list[str] = []
    outgoing: list[str] = []
    for direction, body in rows.all():
        (incoming if direction == "in" else outgoing).append(body or "")
    return incoming, outgoing


async def _problem_text(db: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    """ПЕРВАЯ реплика клиента — с ней он и пришёл. Это и есть проблема.

    ⚠ БЫЛА ПОСЛЕДНЯЯ, И ЭТО ОКАЗАЛОСЬ НЕВЕРНО. Поймано на стенде 13 августа:
    в комментарий заявки попало «Хорошо. Позвоните перед выездом, пожалуйста» —
    то есть вежливое согласие вместо поломки. Мастер по такой заявке едет, не
    зная, что чинить.

    Разговор устроен одинаково: клиент открывает его тем, что у него сломалось
    («перестал работать интернет, роутер горит красным» — образец живой заявки
    КП №1234567), а к концу там договорённости и благодарности. Нужна первая.
    """
    row = await db.execute(
        select(Message.body)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "in",
            Message.body.is_not(None),
        )
        .order_by(Message.created_at)
        .limit(1)
    )
    text = row.scalar_one_or_none()
    if not text:
        return None
    clean = text.strip()
    return clean if len(clean) <= COMMENT_LIMIT else clean[: COMMENT_LIMIT - 1] + "…"


def _lead_payload(
    conv: Conversation,
    client: Client,
    account: AvitoAccount,
    city: str,
    comment: str | None,
    *,
    phone: str,
    src_key: str,
    needs_call: bool = False,
    call_before_visit: bool = False,
    review: bool = False,
) -> dict[str, Any]:
    """Лид в том виде, в каком его ждёт расширение (README, «Протокол»).

    Имена полей — ЕГО, а не наши: `srcKey`, `cityName`, `firstName`. Переводить
    их в наш стиль значило бы заставить чужой готовый код подстраиваться под нас
    ради красоты — и сломать его на первом же несовпадении.
    """
    return {
        # `uid` — идентификатор диалога. По нему расширение гарантирует
        # однократность, и он же приходит обратно в подтверждении.
        "uid": str(conv.id),
        # НЕ `account.lead_src_key`: направление приходит РАЗРЕШЁННЫМ ПО ДИАЛОГУ —
        # из объявления, в которое написал клиент. Разбор — у вызова `lead_direction`.
        "srcKey": src_key,
        # ⚠ НЕ `client.phone`. Номер приходит РАЗРЕШЁННЫМ ПО ДИАЛОГУ (`lead_phone`):
        # у объединённой карточки в `phone` может лежать номер соседнего диалога, и
        # заявка увезла бы мастера к постороннему. Разбор — в докстринге `lead_phone`.
        "phone": phone,
        "firstName": client.name,
        "cityName": city,
        # Что случилось — словами клиента. Без этого мастер едет вслепую.
        "comments": comment,
        # Заголовок объявления: по нему в лид-центре видно, с чего начался
        # разговор. В `advertTitle` не кладём — там ждут название рекламной
        # кампании их справочника, а у нас объявление Авито, это разные вещи.
        "itemTitle": conv.item_title,
        # Номер партнёра, КАК ЕГО ЗНАЮТ ЛЮДИ. Внутренний id расширение находит
        # само по справочнику живой формы: они почти нигде не совпадают, и
        # подстановка номера как id отдала бы заявку ЧУЖОМУ партнёру.
        "partnerNumber": account.lead_partner_number or None,
        # Источник («В95») — наша пометка, уходит в «Комментарий Партнера».
        "partnerComment": account.lead_origin or None,
        # Галочки формы заявки. Имена — как в лид-центре
        # (`is_need_call_check`, `is_need_call_before_visit`, `is_req_fback`),
        # чтобы расширению не пришлось гадать, что во что класть.
        "needCall": needs_call,
        "needCallBeforeVisit": call_before_visit,
        "reqFeedback": review,
    }


async def _review_partners(db: AsyncSession) -> list[str]:
    """Номера партнёров, которым ставится «Отзыв». Настройкой, а не константой."""
    from app.services import app_settings

    values = await app_settings.get_all(db)
    raw = values.get(app_settings.LEADS_REVIEW_PARTNERS) or ""
    return [part.strip() for part in str(raw).split(",") if part.strip()]


@dataclass(frozen=True)
class Batch:
    """Порция лидов и то, что отдать не смогли."""

    leads: list[dict[str, Any]]
    handouts: list[LeadHandout]
    held_back: list[HeldBack]


async def collect(db: AsyncSession, *, limit: int, now: datetime | None = None) -> Batch:
    """Собрать порцию лидов. НИЧЕГО НЕ ЗАПИСЫВАЕТ — решает вызывающий."""
    moment = now or datetime.now(UTC)
    # Диалоги берём ПЕРВЫМИ: замок на выдачи сужается до их идентификаторов, а
    # для этого их надо знать. Порядок безопасен — `_visit_conversations`
    # ничего не пишет и от `handed` не зависит.
    диалоги = await _visit_conversations(db)
    handed = await _already_handed(db, moment, [conv.id for conv, _, _ in диалоги])
    # Настройку читаем один раз на всю порцию: она одна на систему, а лидов в
    # порции до сотни.
    review_partners = await _review_partners(db)

    leads: list[dict[str, Any]] = []
    handouts: list[LeadHandout] = []
    held: list[HeldBack] = []

    for conv, client, account in диалоги:
        previous = handed.get(conv.id)
        if not _needs_handout(previous, moment):
            continue

        # Причины проверяются ВСЕ и по порядку — от самой частой. Первая же
        # несостыковка останавливает лид, но человек видит именно её, а не
        # общее «не получилось».
        # ⚠ ТЕЛЕФОН БЕРЁТСЯ ПО ДИАЛОГУ, А НЕ ИЗ КАРТОЧКИ. Разбор `lead_phone` — там же.
        # Коротко: после объединения карточек в `client.phone` может лежать номер из
        # соседнего диалога, и мастер поедет к другому человеку.
        phone = await lead_phone(db, conv, client)
        if not phone:
            reason = (
                HeldBack.NO_PHONE if not (client.phone or "").strip() else HeldBack.PHONE_AMBIGUOUS
            )
            held.append(HeldBack(conv.id, client.name, account.title, reason))
            continue
        # ⚠ НАПРАВЛЕНИЕ РЕШАЕТ ПРОБЛЕМА КЛИЕНТА (решение владельца 15.08: «бот должен
        # определять направление исходя из проблемы клиента» — оно ПЕРЕВЕРНУЛО
        # порядок 14.08 «читаем объявление»).
        #
        # Живые диалоги показали: клиент пишет в ближайший открытый канал, не выбирая
        # объявление по теме, — про стиральную машину в чат про компьютеры. Слова его
        # реплик честнее названия объявления. Объявление — второй источник (текст без
        # слов техники направления не даёт), настройка канала — третий. Не сработали
        # все три — заявка придерживается, как и раньше: молчание дешевле заявки,
        # уехавшей в чужой лид-центр к мастеру, который не приедет.
        incoming, outgoing = await _dialog_texts(db, conv.id)
        src_key = (
            lead_direction.detect_from_problem("\n".join(incoming))
            or lead_direction.detect(conv.item_title)
            or account.lead_src_key
        )
        if not src_key:
            held.append(HeldBack(conv.id, client.name, account.title, HeldBack.NO_SRC))
            continue
        # ⚠ ЗАПАСНОЙ ПУТЬ ПО ССЫЛКЕ ОБЪЯВЛЕНИЯ (28.08). Здесь читалась ТОЛЬКО
        # колонка, и при пустой колонке заявка молча уходила в придержку «город
        # не распознан». Все остальные потребители города давно спрашивают ещё и
        # ссылку: `conversations.item_view` и `bots.engine.город_диалога` — у
        # последнего в докстринге написано «запасной путь был везде, кроме бота;
        # единственным местом без него оказался путь с самой высокой ценой
        # ошибки». Мест было ДВА: сторож этой правки проверял бота и до выдачи
        # заявок не дошёл.
        #
        # Замер боевой базы 26.08: `item_city_slug` пуст у 4832 диалогов из
        # 15 970 (30 %), и у 4710 из них город лежит прямо в `item_url`. То есть
        # почти треть заявок придерживалась по причине, которую можно снять,
        # прочитав ссылку, — а в шапке чата у диспетчера при этом стоял чип с
        # названием города.
        city = city_by_slug(conv.item_city_slug) or city_by_slug(
            parse_listing_url(conv.item_url).city_slug
        )
        if not city:
            held.append(HeldBack(conv.id, client.name, account.title, HeldBack.NO_CITY))
            continue
        # ⚠ «Белые» заявки партнёра 723 на БТ НЕ создаются (регламент 15.08, п. 3:
        # «можно передавать с КП на МНЧ; на БТ белые заявки не создавать»).
        # Молчаливая придержка с причиной — человек решит, куда её деть.
        white = lead_flags.normalize_partner(account.lead_partner_number) == "723"
        if white and src_key == lead_direction.BT:
            held.append(HeldBack(conv.id, client.name, account.title, HeldBack.WHITE_BT))
            continue

        # Комментарий — как в живой заявке: проблема, что мы озвучили,
        # «Создана с чата», ссылка (у 723 — с подписью «Белый акк» и «БЕЛАЯ
        # ЗАЯВКА»); сборка и порядок — `app/services/lead_comment.py`.
        comment = lead_comment.build(
            problem=await _problem_text(db, conv.id),
            outgoing=outgoing,
            review_url=account.review_url,
            white=white,
        )
        # Галочки прозвона — ТОЛЬКО по словам клиента: предлагать звонок самим
        # регламент запрещает (`app/services/lead_flags.py`).
        needs_call, before_visit = lead_flags.detect_calls(incoming)
        leads.append(
            _lead_payload(
                conv,
                client,
                account,
                city,
                comment,
                phone=phone,
                src_key=src_key,
                needs_call=needs_call,
                call_before_visit=before_visit,
                review=lead_flags.wants_review(
                    account.lead_partner_number, review_partners, src_key=src_key
                ),
            )
        )
        handouts.append(
            LeadHandout(
                id=previous.id if previous else uuid.uuid4(),
                conversation_id=conv.id,
                handed_at=moment,
                # ⚠ ТО ЖЕ НАПРАВЛЕНИЕ, ЧТО УЕХАЛО В ЗАЯВКУ, а не настройка канала.
                # Разъедься эти два места — и журнал выдач говорил бы, что заявка ушла
                # в один лид-центр, тогда как расширение получило другой. Разбираться
                # в этом будут ровно тогда, когда заявка потеряется.
                src_key=src_key,
            )
        )
        if len(leads) >= limit:
            break

    return Batch(leads=leads, handouts=handouts, held_back=held)


async def hand_out(db: AsyncSession, batch: Batch, *, now: datetime | None = None) -> None:
    """Записать факт выдачи. Повторная выдача обновляет время, а не плодит строку."""
    moment = now or datetime.now(UTC)
    for handout in batch.handouts:
        existing = (
            await db.execute(
                select(LeadHandout).where(LeadHandout.conversation_id == handout.conversation_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(handout)
        else:
            existing.handed_at = moment
            existing.attempts = (existing.attempts or 0) + 1
            existing.src_key = handout.src_key
    await db.flush()


async def ack(
    db: AsyncSession,
    *,
    uid: str,
    decision: str,
    request_id: str | None,
    message: str | None,
    now: datetime | None = None,
) -> LeadHandout | None:
    """Принять результат от расширения. `None` — такого лида мы не отдавали."""
    try:
        conversation_id = uuid.UUID(uid)
    except ValueError:
        return None

    row = (
        await db.execute(select(LeadHandout).where(LeadHandout.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if row is None:
        return None

    row.acked_at = now or datetime.now(UTC)
    # Чужое значение приводим к «error», а не отвергаем целиком: подтверждение
    # важнее его точности. Отвергни мы его — лид ушёл бы в повтор, и расширение
    # попыталось бы завести заявку ещё раз.
    row.decision = decision if decision in DECISIONS else "error"
    row.request_id = (request_id or "").strip() or None
    row.message = (message or "").strip() or None
    await db.flush()
    log.info(
        "leads.acked",
        conversation_id=str(conversation_id),
        decision=row.decision,
        request_id=row.request_id,
    )
    return row


async def recent(db: AsyncSession, *, limit: int = 100) -> list[LeadHandout]:
    """Последние выдачи, свежие сверху."""
    rows = await db.execute(
        select(LeadHandout).order_by(LeadHandout.handed_at.desc()).limit(max(1, min(limit, 500)))
    )
    return list(rows.scalars().all())


async def stats(db: AsyncSession) -> dict[str, int]:
    """Сводка для экрана: сколько отдано, создано, отклонено, ждёт ответа."""
    total = (await db.execute(select(func.count()).select_from(LeadHandout))).scalar_one()
    unacked = (
        await db.execute(
            select(func.count()).select_from(LeadHandout).where(LeadHandout.acked_at.is_(None))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(LeadHandout.decision, func.count())
            .where(LeadHandout.decision.is_not(None))
            .group_by(LeadHandout.decision)
        )
    ).all()
    summary: dict[str, int] = {"total": int(total), "unacked": int(unacked)}
    for decision, count in rows:
        if decision:
            summary[str(decision)] = int(count)
    return summary


# ------------------------------------------------------------------- токен


#: Ключ в `app_settings`. Своё значение, а не переменная окружения: токен
#: расширения выпускает владелец из интерфейса, а не инженер при развёртывании.
TOKEN_KEY = "leads.token"


#: ⚠ ПРЕДОХРАНИТЕЛЬ ВТОРОГО ПУТИ ЗАЯВОК — ОДИН НА ВСЕ ДВЕРИ (решение владельца
#: 16.08, аудит 19.08 находка L-005).
#:
#: ЧТО ЗАКРЫВАЕТ. Заявки в CRM сегодня создаёт очередь ЛИД-БОТА через расширение
#: «Автозаявки» на офисном ПК. У LeadChat есть своя очередь тех же бот-диалогов
#: (`GET /leads`), и она ведёт в то же расширение. Включи кто-то второй путь —
#: каждая заявка уехала бы в лид-центр дважды, то есть мастер выехал бы дважды.
#: Расширение проверяет телефон в лид-центре перед созданием (исход `blocked`),
#: но это последний рубеж и он про один номер, а не про поток.
#:
#: ПОЧЕМУ КОНСТАНТА, А НЕ `raise` В РУЧКЕ, КАК БЫЛО. Дверей оказалось две:
#: ручка `POST /settings/leads/token` (её закрыли 16.08) и команда
#: `python -m app.cli leads-token`, написанная ДО этого решения и никем не
#: закрытая. Команда живёт на сервере — то есть ровно там, куда пойдёт человек,
#: которому «надо просто подключить расширение». Предохранитель, стоящий на
#: одной двери из двух, — это не предохранитель.
#:
#: КАК СНИМАТЬ. Поставить `None` — здесь, один раз. Но сперва выключить очередь
#: на стороне лид-бота (`crm.autocreate` в его config.json), иначе оба пути
#: окажутся живыми одновременно, и это будет ровно та беда, ради которой
#: предохранитель и стоит.
SECOND_PATH_FUSE: str | None = (
    "Путь отключён: заявки создаются через очередь лид-бота — второй путь дал бы "
    "дубль каждой заявки в CRM. Сначала выключите очередь бота (crm.autocreate), "
    "затем снимите предохранитель leads.SECOND_PATH_FUSE."
)


async def get_token(db: AsyncSession) -> str:
    """Токен расширения. Пустая строка — не настроено, и ручки закрыты.

    ⚠ ХРАНИТСЯ ЗАШИФРОВАННЫМ тем же ключом, что и токены Авито. Он открывает
    доступ к телефонам клиентов: расширение получает по нему имя, номер и город
    каждого лида. В открытом виде в базе он был бы виден любому, кто дотянулся
    до дампа.
    """
    from app.models.app_setting import AppSetting
    from app.services import crypto

    row = await db.get(AppSetting, TOKEN_KEY)
    if row is None or not isinstance(row.value, dict):
        return ""
    raw = row.value.get("token_enc")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return crypto.decrypt_token(base64.b64decode(raw))
    except (crypto.DecryptError, ValueError):
        # Ключ шифрования сменился (восстановление копии на новом сервере) —
        # токен нечитаем. Молчать нельзя: снаружи это выглядит как «расширение
        # перестало пускать», а причина ровно здесь.
        log.warning("leads.token_unreadable")
        return ""


async def set_token(db: AsyncSession, token: str) -> str:
    """Сохранить токен. Пустая строка — выключить автозаявки.

    Возвращает то, что сохранили, — чтобы вызывающий мог показать его человеку
    ОДИН раз. Второго раза не будет: наружу токен больше не отдаётся.
    """
    from app.models.app_setting import AppSetting
    from app.services import crypto

    clean = (token or "").strip()
    if clean and not clean.isascii():
        # Та же беда, что у токена лид-бота: заголовки HTTP — latin-1, и
        # кириллица в токене роняет запрос ещё до сети. Снаружи это выглядит
        # как «расширение не работает», а повод самый бытовой.
        raise ValueError("Токен может состоять только из латиницы, цифр и знаков")

    value = {"token_enc": base64.b64encode(crypto.encrypt_token(clean)).decode()} if clean else {}
    row = await db.get(AppSetting, TOKEN_KEY)
    if row is None:
        db.add(AppSetting(key=TOKEN_KEY, value=value))
    else:
        row.value = value
    await db.flush()
    return clean


def new_token() -> str:
    """Придумать токен. 32 байта — заведомо неподбираемо, и читается в одну строку."""
    return secrets.token_urlsafe(32)
