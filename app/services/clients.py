"""Карточка клиента: телефон руками, объединение и разъединение карточек.

ОТКУДА ЭТО ВЗЯЛОСЬ. Автоматическая склейка карточек на данных Авито
принципиально ненадёжна, и это измерено, а не предположено: на 12 августа в
боевой базе 37 обращений и телефон известен у ТРЁХ. Второй опоры нет — Авито
нигде не обещает, что `author_id` общий для разных наших аккаунтов (в
спецификации у `Chat.users[].id` стоит ровно «Обратите внимание на
хэширование»). То есть в 34 случаях из 37 автоматике не на что опереться, а
единственный, кто видит, что «Ольга с ноутбуком» и «Ольга со стиралкой» — один
человек, это диспетчер, читающий переписку.

Ошибка в обратную сторону уже стоила дорого: 11 августа молчаливая склейка по
совпавшему идентификатору собрала под одним именем восемь человек из разных
городов. Отсюда три правила, которые здесь соблюдаются буквально.

1. МОЛЧА НИЧЕГО НЕ СКЛЕИВАЕТСЯ. Совпадение телефона, идентификатора или имени
   даёт ПОДСКАЗКУ с кнопкой (:func:`merge_candidates`), а не операцию. Даже
   совпавший нормализованный телефон — которому правило разрешает автоматику —
   здесь требует нажатия: объединение переносит все диалоги человека, и
   случиться это должно как поступок оператора, а не как побочный эффект
   ввода номера.
2. ОБЪЕДИНЕНИЕ НЕ РАЗРУШАЕТ ДАННЫЕ. Проигравшая строка `clients` остаётся
   жить с пометкой `merged_into_id` (почему именно так — в
   `app/models/client.py`), диалоги переезжают на победителя, телефоны и
   идентификаторы обеих карточек показываются СПИСКОМ: у одного человека их
   законно несколько, по одному на каждый наш аккаунт.
3. «РАЗЪЕДИНИТЬ» ВОЗВРАЩАЕТ ТОЧНО ИСХОДНОЕ СОСТОЯНИЕ. Всё, что операция
   меняет, она сначала записывает в журнал аудита (`client.merged`,
   `details.before`) — вместе со списком переехавших диалогов. Разъединение
   читает этот снимок и возвращает поля и диалоги ровно те и ровно туда.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Автоматика склейки (`_upsert_client`,
`_apply_phone_evidence` в `app/services/inbound.py`) не трогается вовсе: она
чужая зона и работает по своим правилам. Поля `link_confidence`,
`cross_account_since`, `link_phone_conflict_at` описывают АВТОМАТИЧЕСКИЕ
свидетельства, и объединение руками их не переписывает — иначе карточка,
сведённая человеком, объясняла бы себя словами «совпал идентификатор Авито»,
то есть врала бы о происхождении связи. Ручное объединение показывается своими
словами и со своей мерой доверия (:func:`identity_view`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.errors import ApiError
from app.models import (
    AuditLog,
    Client,
    ClientAddressCandidate,
    ClientMergeVeto,
    ClientPhoneCandidate,
    Conversation,
    User,
)
from app.models.client import (
    CANDIDATE_ACCEPTED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CANDIDATE_SOURCE_INBOUND,
    CANDIDATE_SOURCE_SWAP,
    CANDIDATE_SOURCE_VOICE,
)
from app.services import address_parse, app_settings, dialect, geocode, phone_parse, phone_rules
from app.services.audit import write_audit

log = structlog.get_logger("app.clients")

# --- телефон -----------------------------------------------------------------


def normalize_phone(raw: str | None) -> str | None:
    """Ручной ввод → ``+7XXXXXXXXXX``. Непохожее на номер — ``None``.

    Вход в :func:`app.services.phone_parse.normalize`; имя сохранено, потому что
    по нему сюда ходят ручка карточки и тесты. Раньше здесь лежала ВТОРАЯ
    реализация нормализации — с комментарием «разойтись им нельзя», — и такая
    же третья в `inbound.extract_phone`. Разъезд стоил бы того, что номер,
    вычитанный из переписки, и тот же номер, введённый руками, легли бы в базу
    двумя разными строками и перестали бы совпадать при поиске двойников, то
    есть объединение карточек по телефону не сработало бы там, где данных
    больше всего. Одна реализация расходиться не умеет.
    """
    return phone_parse.normalize(raw)


async def phone_twins(db: AsyncSession, phone: str, *, exclude_id: uuid.UUID) -> list[Client]:
    """Другие карточки с ЭТИМ ЖЕ номером — кроме уже объединённых.

    Объединённые исключены не для краткости: предложить объединить карточку с
    той, которая уже объединена в третью, значит предложить цепочку — а её
    «Разъединить» распутать не сможет, потому что снимок в журнале описывает
    одну операцию, а не дерево.
    """
    return list(
        (
            await db.execute(
                sa.select(Client).where(
                    Client.phone == phone,
                    Client.id != exclude_id,
                    Client.merged_into_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


# --- телефон, распознанный в тексте сообщения (правка 10 от 12 августа) -------
#
# ЗАЧЕМ ЭТОТ РАЗДЕЛ. Номер, написанный внутри обычной фразы («Прошу сообщить о
# времени прихода за 1 час в СМС по номеру : +7(900)1112240.»), в карточку не
# попадал: лид с готовым контактом лежал в переписке, а карточка показывала
# кнопку «указать телефон» и поиск по телефону его не находил.
#
# ПОЧЕМУ РАСПОЗНАННОЕ НЕ ПИШЕТСЯ ПРЯМО В КАРТОЧКУ. Потому что разбор ошибается
# на цифрах, телефоном не являющихся, а номер из карточки набирают и диктуют
# мастеру вслух: ошибка стоит звонка постороннему человеку и потерянного
# настоящего клиента. Поэтому по умолчанию распознанное становится КАНДИДАТОМ —
# предложением с действиями «заменить», «добавить», «отклонить», — и решает
# человек. Руководитель может включить запись сразу
# (`app_settings.PHONE_DETECT_AUTOFILL`), и тогда пустая карточка заполняется
# без вопросов, а непустая по-прежнему не перезаписывается никогда.


async def record_phone_candidate(
    db: AsyncSession,
    *,
    client: Client,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    message_at: datetime | None,
    found: phone_parse.Found,
    source: str = CANDIDATE_SOURCE_INBOUND,
    accepted: bool = False,
    now: datetime,
    hint: str | None = None,
) -> bool:
    """Запомнить распознанный номер. ``True`` — строка появилась впервые.

    ВСТАВКА «ИЛИ НИЧЕГО», А НЕ «НАЙТИ И ДОБАВИТЬ». Клиент пишет свой телефон в
    каждом втором сообщении, и два вебхука одного диалога обрабатываются
    параллельно: проверка «нет ли уже такой строки» с последующей вставкой
    рано или поздно упёрлась бы в нарушение уникальности, а это потерянный
    вебхук — сообщение, которого клиент так и не дождётся.

    ПОВТОР НИЧЕГО НЕ МЕНЯЕТ, И ЭТО ГЛАВНОЕ СВОЙСТВО. Отклонённый оператором
    номер не возвращается предложением после каждого следующего сообщения с
    тем же номером — иначе люди перестали бы читать подсказки за неделю.
    """
    insert = dialect.insert(db)
    result = await db.execute(
        insert(ClientPhoneCandidate)
        .values(
            id=uuid.uuid4(),
            client_id=client.id,
            conversation_id=conversation_id,
            message_id=message_id,
            message_at=message_at,
            phone=found.value,
            raw=found.raw,
            source=source,
            status=CANDIDATE_ACCEPTED if accepted else CANDIDATE_PENDING,
            detected_at=now,
            # Автоматическая запись в пустую карточку — решение системы, а не
            # человека: `resolved_by_id` остаётся пустым, и карточка честно
            # скажет «из диалога», а не «подтвердил Иванов».
            resolved_at=now if accepted else None,
            hint=hint,
        )
        .on_conflict_do_nothing(index_elements=["client_id", "phone"])
    )
    return bool(getattr(result, "rowcount", 0) == 1)


class Absorbed(NamedTuple):
    """Что сделала автоматика с номерами одного сообщения."""

    #: Причина кадра `client:updated` — самая сильная из случившихся, или ``None``.
    reason: str | None
    primary_filled: bool
    extras_added: int
    skipped: int


#: Порядок причин кадра по силе: заполненный основной важнее дополнительного.
_ПРИЧИНЫ = ("phone_captured", "phone_extra_added", "phone_suggested")

#: Номер из реплики старше этого — «история» для отчёта «собрано телефонов»
#: (`stats._PHONES_SQL`, 06 §1.4): строка аудита ложится временем записи, а
#: событие случилось, когда клиент назвал номер. Догон истории (N29, 19.09) и
#: `backfill-cards` приносят реплики до 30 дней давности — без признака день
#: подключения канала показывал бы месяц чужих номеров. Сутки — шаг отчёта
#: (день по Москве): сверка вебхуков опаздывает часами, и её номера остаются
#: «сегодняшними». Признак считается по стенным часам, а не по `now` разбора:
#: у догона `now` = время реплики (проект N29 §2.3), и по нему история от живого
#: входящего неотличима.
ИСТОРИЯ_СТАРШЕ = timedelta(hours=24)


def номер_из_истории(message_at: datetime | None, *, wall: datetime) -> bool:
    """Реплика с номером старше `ИСТОРИЯ_СТАРШЕ` на момент записи (`wall` —
    стенные часы). Без времени реплики — не история: как запись до 19.09."""
    return message_at is not None and wall - _к_utc(message_at) > ИСТОРИЯ_СТАРШЕ


async def absorb_phones(
    db: AsyncSession,
    *,
    client: Client,
    conversation_id: uuid.UUID,
    account_id: uuid.UUID,
    message_id: uuid.UUID | None,
    message_at: datetime | None,
    text: str | None,
    found: list[phone_parse.Found],
    now: datetime,
    autofill: bool,
    own: frozenset[str],
    source: str = CANDIDATE_SOURCE_INBOUND,
) -> Absorbed:
    """ЕДИНСТВЕННЫЙ писатель номеров из переписки в карточку (правила 12.09).

    Три фразы владельцу — в шапке `phone_rules`. Здесь их исполнение:

    * первый годный номер сообщения заполняет ПУСТОЙ основной — условным
      `UPDATE … WHERE phone IS NULL`: два вебхука одного человека с двух наших
      аккаунтов приходят одновременно, и проигравший номер ложится
      дополнительным, а не затирает победителя;
    * остальные номера (и любой номер на заполненной карточке) ложатся
      ДОПОЛНИТЕЛЬНЫМИ — принятой строкой без автора, с датой, сообщением и
      словом-подсказкой; заполненный основной не меняется никогда;
    * наши номера и 8-800 не пишутся вовсе;
    * при выключенной автозаписи всё уходит в вопросы оператору, как раньше.

    `ON CONFLICT DO NOTHING` в `record_phone_candidate` остаётся главным
    свойством: повтор номера, который человек отклонил, отклонённым и остаётся;
    повтор принятого ничего не меняет.

    `source` — происхождение строки (`inbound` по умолчанию, `voice` — номер из
    расшифровки голосового): выбирает вызывающий по тому, откуда взят `text`
    (`inbound._maybe_extract_phone`, один судья). Правила записи от источника
    не зависят — только подпись на карточке и замер доверия.
    """
    primary = client.phone
    reason: str | None = None
    primary_filled = False
    extras = 0
    skipped = 0
    годных = 0

    def сильнее(новая: str) -> None:
        nonlocal reason
        if reason is None or _ПРИЧИНЫ.index(новая) < _ПРИЧИНЫ.index(reason):
            reason = новая

    for i, hit in enumerate(found):
        verdict = phone_rules.classify(
            hit.value, index=годных, primary=primary, own=own, autofill=autofill
        )
        if verdict.kind in (phone_rules.SKIP_OWN, phone_rules.SKIP_TOLL_FREE):
            skipped += 1
            log.info(
                "client.phone_skipped",
                client_id=str(client.id),
                conversation_id=str(conversation_id),
                reason=verdict.reason,
            )
            continue
        годных += 1
        if verdict.kind == phone_rules.ALREADY_PRIMARY:
            continue
        hint = phone_rules.hint_word(
            text,
            hit.start,
            hit.end,
            prev_end=found[i - 1].end if i else 0,
            next_start=found[i + 1].start if i + 1 < len(found) else None,
        )
        новая_строка = await record_phone_candidate(
            db,
            client=client,
            conversation_id=conversation_id,
            message_id=message_id,
            message_at=message_at,
            found=hit,
            source=source,
            accepted=verdict.kind != phone_rules.PENDING,
            now=now,
            hint=hint,
        )
        if verdict.kind == phone_rules.PENDING:
            log.info(
                "client.phone_suggested",
                client_id=str(client.id),
                conversation_id=str(conversation_id),
                has_phone=primary is not None,
            )
            сильнее("phone_suggested")
            continue
        if not новая_строка:
            # Решение по этому номеру уже принято — человеком или нами раньше.
            log.info(
                "client.phone_autofill_skipped",
                client_id=str(client.id),
                conversation_id=str(conversation_id),
                reason="решение по этому номеру уже принято",
            )
            continue
        if verdict.kind == phone_rules.FILL_PRIMARY:
            занято = await заполнить_основной(
                db,
                client,
                hit.value,
                conversation_id=conversation_id,
                account_id=account_id,
            )
            if занято:
                primary = hit.value
                primary_filled = True
                сильнее("phone_captured")
                await write_audit(
                    db,
                    user_id=None,
                    action="client.phone_captured",
                    entity="client",
                    entity_id=str(client.id),
                    # `source='regex'` — не про регулярку, а про колонку «из них
                    # автоизвлечением» в отчёте «собрано телефонов» (06 §1.4,
                    # `stats._PHONES_SQL`). Переименовать её здесь значило бы
                    # обнулить столбец отчёта задним числом. `history` — номер
                    # из реплики старше суток (`ИСТОРИЯ_СТАРШЕ`): отчёт такие не
                    # считает. Ключ свой, а не `backfill` команды `phone-backlog`:
                    # исключить тот значило бы переписать закрытые месяцы.
                    details={
                        "conversation_id": str(conversation_id),
                        "source": "regex",
                        "message_at": message_at.isoformat() if message_at else None,
                        "history": номер_из_истории(message_at, wall=datetime.now(UTC)),
                    },
                )
                continue
            # Проиграли гонку за пустой основной: строка уже принята — это
            # дополнительный номер, и подпись у него обязана быть такой же.
            primary = client.phone
        extras += 1
        сильнее("phone_extra_added")
        await write_audit(
            db,
            user_id=None,
            action="client.phone_candidate_accepted",
            entity="client",
            entity_id=str(client.id),
            details={
                "phone": hit.value,
                "conversation_id": str(conversation_id),
                "message_id": str(message_id) if message_id else None,
                "message_at": message_at.isoformat() if message_at else None,
                "primary": False,
                "auto": True,
                "rule": "extra_v2",
                "hint": hint,
                "near_primary": phone_rules.near_duplicate(primary, hit.value),
            },
        )
    return Absorbed(reason, primary_filled, extras, skipped)


async def заполнить_основной(
    db: AsyncSession,
    client: Client,
    phone: str,
    *,
    conversation_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Условная запись основного: только если он ПУСТ в базе прямо сейчас.

    ORM-объект после сырого UPDATE перечитывается, иначе журнал и кадр
    описывали бы старое состояние (урок автозаписи адреса, `workers/geocode.py`).
    """
    result = await db.execute(
        sa.update(Client)
        .where(Client.id == client.id, Client.phone.is_(None))
        .values(
            phone=phone,
            phone_account_id=account_id,
            phone_conversation_id=conversation_id,
        )
        .execution_options(synchronize_session=False)
    )
    занято = getattr(result, "rowcount", 0) == 1
    await db.refresh(client, ["phone", "phone_account_id", "phone_conversation_id"])
    return занято


async def set_address(
    db: AsyncSession,
    client: Client,
    raw: str,
    *,
    actor: User,
    conversation_id: uuid.UUID | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Записать адрес, введённый руками. Транзакцией владеет вызывающий.

    ⚠ РАЗБОР ЗДЕСЬ НЕ ПРИМЕНЯЕТСЯ, И ЭТО РЕШЕНИЕ. У телефона строка приводится
    к канону `+7XXXXXXXXXX`, потому что канон существует и один. У адреса его
    нет: «Ленина 5», «ул. Ленина, д. 5», «Ленина 5 кв 3» — всё это верные
    записи одного места, и выбирать за человека, какая из них правильная,
    значит спорить с тем, кто только что говорил с клиентом. Храним как
    набрано, обрезая только края.

    Пустая строка стирает адрес — это законное действие: «клиент передумал,
    адрес был не его». `reason` — почему стёрли, когда это не просто правка, а
    названное действие («Адрес неверный», `reject_auto_address`): попадает в
    журнал вместе со строкой-источником, сама арифметика стирания одна.
    """
    адрес = (raw or "").strip()
    if len(адрес) > MAX_ADDRESS_LEN:
        raise ApiError(
            "validation_error",
            f"Адрес длиннее {MAX_ADDRESS_LEN} символов — вряд ли это адрес.",
            status=422,
        )
    previous = client.address
    if previous == (адрес or None):
        return {"address": client.address, "changed": False}

    # ⚠ СТИРАНИЕ АДРЕСА, ПОСТАВЛЕННОГО АВТОМАТИКОЙ, — ЭТО ОТКАЗ ОТ СТРОКИ.
    # Иначе петля: оператор стёр, клиент написал «кв 3», карта снова
    # подтвердила ту же строку, автозапись вернула адрес. Память об отказе
    # живёт в строке-кандидате — той же, что помнит «это не адрес».
    отклонена: ClientAddressCandidate | None = None
    if not адрес and client.address_candidate_id is not None:
        источник = await db.get(ClientAddressCandidate, client.address_candidate_id)
        if источник is not None and источник.resolved_by_id is None:
            источник.status = CANDIDATE_REJECTED
            источник.resolved_at = datetime.now(UTC)
            источник.resolved_by_id = actor.id
            отклонена = источник

    client.address = адрес or None
    client.address_set_by_id = actor.id if адрес else None
    client.address_set_at = datetime.now(UTC) if адрес else None
    client.address_conversation_id = conversation_id if адрес else None
    # Набранное руками не происходит ни из какой строки: связь снимается,
    # иначе карточка показывала бы под чужим текстом чужую цитату.
    client.address_candidate_id = None
    client.address_value = None
    details: dict[str, Any] = {
        "source": "manual",
        "previous": previous,
        "address": client.address,
        "conversation_id": str(conversation_id) if conversation_id else None,
    }
    if reason:
        # Названное действие («Адрес неверный», пакет 6.0а I-9) отличимо от
        # обычного стирания причиной и строкой-источником: по ним воронка
        # считает отказы руками по правилу (`trace.rule` строки), а простая
        # правка остаётся правкой — у неё этих ключей нет.
        details["reason"] = reason
        details["candidate_id"] = str(отклонена.id) if отклонена is not None else None
    await write_audit(
        db,
        user_id=actor.id,
        # Первое заполнение и правка — разные события, как у телефона: одна
        # опечатка, поправленная трижды, не должна выглядеть как три собранных
        # адреса.
        action="client.address_captured" if previous is None else "client.address_edited",
        entity="client",
        entity_id=str(client.id),
        details=details,
    )
    await _снять_совпавшие_адреса(db, client, client.address, actor=actor)
    return {"address": client.address, "changed": True}


async def _снять_совпавшие_адреса(
    db: AsyncSession, client: Client, адрес: str | None, *, actor: User
) -> None:
    """Оператор ввёл руками то же, что предлагала система, — вопрос снят.

    Без этого карточка выглядела бы издевательски: адрес уже стоит в поле, а
    под ним по-прежнему висит предложение «распознан адрес …, подтвердить или
    отклонить». Отдельного действия человек тут не совершал, поэтому и
    отдельной строки журнала нет.
    """
    if not адрес:
        return
    now = datetime.now(UTC)
    ровно = _ровно(адрес)
    for row in await address_candidates(db, client.id, only_pending=True):
        # Совпадение — и с «улица, дом» клиента, и со строкой карты: оператор
        # мог перепечатать любую из двух, что видел в предложении.
        if ровно not in {_ровно(row.value), _ровно(row.geo_formatted)}:
            continue
        row.status = CANDIDATE_ACCEPTED
        row.resolved_at = now
        row.resolved_by_id = actor.id


def _ровно(s: str | None) -> str:
    return " ".join((s or "").lower().split())


#: Причина в журнале у нажатия «Адрес неверный» — по ней воронка отличает отказ
#: от автоадреса от обычной правки руками (`client.address_edited`).
ADDRESS_WRONG_REASON = "wrong"


async def reject_auto_address(
    db: AsyncSession,
    client: Client,
    *,
    actor: User,
    conversation_id: uuid.UUID | None,
) -> dict[str, Any]:
    """«Адрес неверный» — одно нажатие под записанным автоадресом (пакет 6.0а, I-9).

    Делает ровно то же, что «изменить» → пустая строка (`set_address("")`):
    строка-источник → `rejected` + `resolved_by_id` (место закрыто для
    автоматики), карточка → пусто. Отдельной арифметики стирания здесь нет
    намеренно — два пути к одному полю в этом проекте расходились трижды;
    отличие одно — причина в журнале, по которой воронка считает отказы
    руками по правилу.

    Только адрес АВТОМАТИКИ. Набранный руками (`address_set_at`) или принятый
    человеком (`replace` — у источника `resolved_by_id`) отвергать нажатием
    некому: это спор двух людей, и решается он через «изменить». 409 — честный
    ответ устаревшему экрану: коллега уже исправил или подтвердил адрес, и
    молча стереть его работу нельзя.
    """
    источник = (
        await db.get(ClientAddressCandidate, client.address_candidate_id)
        if client.address_candidate_id is not None
        else None
    )
    if client.address is None or источник is None or источник.resolved_by_id is not None:
        raise ApiError(
            "address_not_auto",
            "Адрес записан не автоматикой — исправьте его через «изменить»",
            status=409,
        )
    return await set_address(
        db,
        client,
        "",
        actor=actor,
        conversation_id=conversation_id,
        reason=ADDRESS_WRONG_REASON,
    )


async def resolve_address_candidate(
    db: AsyncSession,
    *,
    candidate: ClientAddressCandidate,
    client: Client,
    decision: str,
    actor: User,
    variant: int | None = None,
) -> dict[str, Any]:
    """Решение оператора по распознанному адресу.

    ТРИ ИСХОДА, И ОНИ ТЕ ЖЕ, ЧТО У ТЕЛЕФОНА — слова взяты из постановки
    владельца; переводить их по дороге в другие значило бы завести четвёртое
    понимание тех же трёх действий. С 18.09 адрес пишет автоматика по степени
    `candidate_grade`; с экрана сюда приходят `reject` («Не адрес») и `replace`
    («Записать в карточку») — у предложения правила и у строки, которую
    автоматика сама не запишет (`writable` в `identity_view`, проверка 24.09).
    `add` остаётся для CLI и тестов.

    * `replace` — «это адрес выезда». Встаёт в `clients.address`; прежний
      затирается, но не пропадает: он остаётся принятой строкой, если сам был
      распознан из переписки.
    * `add` — «адрес его, но выезд по другому». Строка принята, карточка не
      тронута. Это и есть безопасный ответ для второго адреса.
    * `reject` — «это не адрес». Строка остаётся с пометкой: без неё то же
      место предлагалось бы заново после каждого следующего сообщения.
    """
    if decision not in CANDIDATE_DECISIONS:
        raise ApiError(
            "validation_error",
            "Допустимые решения: заменить, добавить, отклонить",
            status=422,
            details={"fields": [{"field": "decision", "rule": "enum", "message": ""}]},
        )
    if candidate.status != CANDIDATE_PENDING:
        raise ApiError("already_resolved", "Решение по этому адресу уже принято", status=409)

    # ВЫБРАННЫЙ ВАРИАНТ КАРТЫ (12.09). Карта не выбрала один дом и показала,
    # что нашла; человек указал, который. Выбранное становится строкой карты
    # этого кандидата — дальше всё как у подтверждённого картой адреса.
    if variant is not None and decision != "reject":
        варианты = list(candidate.geo_variants or [])
        if not (0 <= variant < len(варианты)):
            raise ApiError("validation_error", "Такого варианта нет", status=422)
        выбран = варианты[variant]
        candidate.geo_status = geocode.GEO_EXACT
        candidate.geo_formatted = str(выбран.get("formatted") or "") or None
        candidate.geo_lat = выбран.get("lat")
        candidate.geo_lon = выбран.get("lon")

    now = datetime.now(UTC)
    candidate.status = CANDIDATE_REJECTED if decision == "reject" else CANDIDATE_ACCEPTED
    candidate.resolved_at = now
    candidate.resolved_by_id = actor.id
    # Решение человека — конец пересуда: снимок улик (`geo_prev`) строке больше
    # не нужен, а обход `kept_blind` решённую строку и так не берёт (пакет 5).
    candidate.geo_prev = None
    if decision != "reject" and geocode.point_is_suggest(candidate.geo_provider):
        # Предложение правила (политика `suggest`, пакет 6.0а) принято человеком:
        # хвост снимается, и степень строки становится той, что записало правило
        # (`~approx` остаётся); `trace.suggest` остаётся — по нему воронка
        # считает принятые руками предложения.
        candidate.geo_provider = geocode.strip_suggest(candidate.geo_provider)
    if decision != "replace":
        # «Добавить» и «Не адрес» карточку не меняют — и именно поэтому их надо
        # записать. Без строки журнала оба решения выглядят как бездействие, а
        # вопрос «кто сказал, что это не адрес» остаётся без ответа: у соседнего
        # телефона обе строки есть с 12 августа.
        await write_audit(
            db,
            user_id=actor.id,
            action=(
                "client.address_candidate_rejected"
                if decision == "reject"
                else "client.address_candidate_accepted"
            ),
            entity="client",
            entity_id=str(client.id),
            details={
                "address": candidate.value,
                "raw": candidate.raw,
                "conversation_id": str(candidate.conversation_id),
                "variant": variant,
            },
        )
    if decision == "replace":
        # Прежний адрес, введённый руками, при замене теряется — ровно как у
        # телефона, и по тому же доводу: «заменить» кнопкой и «заменить» руками
        # обязаны делать одно и то же.
        await apply_candidate_to_card(db, client, candidate, actor_id=actor.id, source="regex")
    return {"address": client.address, "candidate": address_candidate_view(candidate)}


class _Unset:
    """Метка «аргумент не передан» — `None` здесь значимое значение."""


_UNSET = _Unset()


async def apply_candidate_to_card(
    db: AsyncSession,
    client: Client,
    candidate: ClientAddressCandidate,
    *,
    actor_id: uuid.UUID | None,
    source: str,
    previous: str | None | _Unset = _UNSET,
) -> None:
    """Положить строку распознавания в карточку. ЕДИНСТВЕННЫЙ путь для этого.

    ⚠ ОДИН ПУТЬ НА «ПОДТВЕРДИТЬ» И НА АВТОЗАПИСЬ (спор проекта 11.09). Пока
    кнопка писала `candidate.value`, а автозапись собиралась писать строку
    карты, одно и то же решение давало два разных текста в одном поле — и
    четыре места, сравнивавших поле со строками, разошлись бы. Текст собирает
    `candidate_address_text`: строка карты при степени (`candidate_grade`),
    иначе «улица, дом» клиента, и части адреса всегда — до 11.09 кнопка
    теряла квартиру.

    Происхождение — по `address_candidate_id`, а не по тексту. Отметок ручного
    ввода нет: иначе карточка подписала бы распознанное словами «со слов».
    `actor_id` пуст у автозаписи — карточка честно скажет «автоматически».
    `previous` передаёт автозапись: она пишет условным UPDATE до этого вызова,
    и ORM уже видит новый текст — без явного «было пусто» журнал сказал бы
    «исправлен» о первом заполнении.
    """
    if isinstance(previous, _Unset):
        previous = client.address
    client.address = candidate_address_text(
        candidate,
        # Части — со всех строк того же места: показ их сливает, и запись
        # обязана совпасть с показом (ревью 13.09).
        parts=await _части_места(db, candidate),
    )
    client.address_candidate_id = candidate.id
    # Ключ «улица, дом» хранится и на карточке: строка-источник уходит каскадом
    # вместе с диалогом (отключение канала чистит историю), а «этот адрес уже в
    # карточке» обязано узнаваться и после этого (ревью 11.09).
    client.address_value = candidate.value
    client.address_set_by_id = None
    client.address_set_at = None
    client.address_conversation_id = candidate.conversation_id
    await write_audit(
        db,
        user_id=actor_id,
        action="client.address_captured" if previous is None else "client.address_edited",
        entity="client",
        entity_id=str(client.id),
        details={
            "source": source,
            "previous": previous,
            "address": client.address,
            "conversation_id": str(candidate.conversation_id),
            "candidate_id": str(candidate.id),
            "geo_status": candidate.geo_status,
            "precision": candidate_precision(candidate),
        },
    )


async def candidate_belongs(
    db: AsyncSession, client: Client, candidate: ClientAddressCandidate | ClientPhoneCandidate
) -> bool:
    """Строка-кандидат принадлежит карточке — своей или присоединённой к ней.

    Владелец 18.09 (Ангарск): карточку объединила автоматика по телефону, а
    «Подтвердить» на адресе присоединённой карточки отвечало «Предложение не
    найдено». Кандидаты при объединении намеренно не переезжают (решение
    19.08: нечего было бы возвращать при разъединении), зато показ карточки
    собирает их со всей группы — значит и решение по ним обязано приниматься
    с той же карточки, по тому же определению «чьи это строки».
    """
    return candidate.client_id in await _группа_карточек(db, client.id, include_merged=True)


async def _группа_карточек(
    db: AsyncSession, client_id: uuid.UUID, *, include_merged: bool
) -> list[uuid.UUID]:
    """Карточка и всё, что к ней присоединено, — вглубь по цепочке.

    ⚠ ОДНО ОПРЕДЕЛЕНИЕ НА ОБЕ ТАБЛИЦЫ КАНДИДАТОВ. Телефон и адрес отвечают на
    один и тот же вопрос «чьи это строки», и две копии обхода разошлись бы:
    в этом проекте класс «два пути считают одно поле по-разному» встречался уже
    трижды.

    Потолок глубины намеренный — кольцо в данных не должно вешать чтение.
    """
    ids = [client_id]
    if not include_merged:
        return ids
    рубеж = [client_id]
    for _ in range(5):
        рубеж = [c.id for c in await _merged_into_children(db, рубеж)]
        if not рубеж:
            break
        ids.extend(рубеж)
    return ids


#: Потолок длины адреса. Адрес показывается в карточке и уезжает в заявку;
#: строка длиннее этого — почти наверняка не адрес, а вставленный кусок
#: переписки.
MAX_ADDRESS_LEN = 300

#: Части адреса, которые дописываются следующими сообщениями.
_ЧАСТИ_АДРЕСА: tuple[str, ...] = ("office", "entrance", "floor", "intercom")


class RecordedAddress(NamedTuple):
    """Итог записи распознанного адреса — см. :func:`record_address_candidate`."""

    впервые: bool
    candidate_id: uuid.UUID | None
    перепроверить: bool


#: Форма снимка улик пересуда (`geo_prev`, пакет 5) — один список ключей на
#: строителя (`снимок_улик`) и читателя (воркер). `content` — ключ содержания
#: строки: разбор изменился под снимком (пункт дописан старой репликой, массив
#: уточнён, `reparse`) — снимок про другую строку, воркер его стирает (ревью
#: 20.09, п. 3). `missing` — карты, без которых вынесен удержанный/слепой
#: вердикт; `rank` — `geocode.verdict_rank` на момент снимка.
СНИМОК_УЛИК: tuple[str, ...] = (
    "status",
    "formatted",
    "variants",
    "provider",
    "lat",
    "lon",
    "verdict_version",
    "without_dadata",
    "checked_at",
    "reason",
    "rank",
    "missing",
    "content",
)
#: Поля строки, по которым снимок узнаёт «та же строка».
КЛЮЧ_СОДЕРЖАНИЯ: tuple[str, ...] = ("value", "settlement", "settlement_type", "locality", "area")


def ключ_содержания(row: ClientAddressCandidate | Mapping[str, Any]) -> dict[str, Any]:
    """Ключ содержания строки для снимка — по ORM-строке или словарю полей."""
    взять = row.get if isinstance(row, Mapping) else lambda k: getattr(row, k)
    return {k: взять(k) for k in КЛЮЧ_СОДЕРЖАНИЯ}


def снимок_улик(
    row: ClientAddressCandidate | Mapping[str, Any],
    *,
    reason: str,
    missing: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Снимок улик вердикта, стоящего в строке, — в `geo_prev` (пакет 5).

    Кладётся и при ранге 0 (`not_found` без улик): снимок — не только «что
    удерживать», но и признак «строка в пересуде и ждёт полного набора карт»,
    по которому обход ставит её в квоту Яндекса; удержать ранг 0 ниже ранга 0
    нельзя, так что вреда нет. Принимает ORM-строку и словарь полей (воркер
    строит снимок из значений, которые сам записывает)."""
    взять = row.get if isinstance(row, Mapping) else lambda k: getattr(row, k)
    когда = взять("geo_checked_at")
    return {
        "status": взять("geo_status"),
        "formatted": взять("geo_formatted"),
        "variants": взять("geo_variants") or None,
        "provider": взять("geo_provider"),
        "lat": взять("geo_lat"),
        "lon": взять("geo_lon"),
        "verdict_version": взять("geo_verdict_version"),
        "without_dadata": bool(взять("geo_without_dadata")),
        "checked_at": когда.isoformat() if isinstance(когда, datetime) else когда,
        "reason": reason,
        "rank": geocode.verdict_rank(
            взять("kind"),
            взять("geo_status"),
            взять("geo_provider"),
            взять("geo_lat"),
            взять("geo_lon"),
            взять("geo_formatted"),
            взять("geo_variants"),
        ),
        "missing": list(missing),
        "content": ключ_содержания(row),
    }


#: «Снимок не трогать» для `сброс_вердикта`: строка сбрасывается не по
#: содержанию, а по внешней причине (смена провайдера снимает бан), и снимок,
#: который в ней уже лежит, — про эту же строку (ревью 20.09, C4).
СНИМОК_НЕ_ТРОГАТЬ: object = object()
#: «След не трогать» для `сброс_вердикта` — умолчание: bulk UPDATE без строки
#: в руках (смена провайдера) следа не знает и оставляет его как есть; суд
#: после сброса сам уведёт прежний `rule` в `prev` (`geocode.verdict_trace`).
СЛЕД_НЕ_ТРОГАТЬ: object = object()


def сброс_вердикта(
    *,
    с_попытками: bool,
    улики: dict[str, Any] | None | object,
    след: dict[str, Any] | None | object = СЛЕД_НЕ_ТРОГАТЬ,
) -> dict[str, Any]:
    """Поля строки при возврате в очередь карты — ОДИН набор на все пути
    (ORM-сброс, bulk UPDATE починки, CLI, ручка настроек).
    `с_попытками=True` — содержание строки то же, повтор по триггеру: счётчик
    остаётся, `ПОТОЛОК_ПОПЫТОК` починки ограничивает число повторов за жизнь
    строки. `False` — строка изменилась (дописан пункт/город/тип): с нуля.
    `geo_checked_at=None` обязателен: `_пора` починки берёт строку сразу, а
    воркерный признак «отложенная без DaData» не срабатывает.
    `улики` — снимок прежнего вердикта (`снимок_улик`) у пересуда того же
    содержания (обход, `address-recheck`); `None` у всех живых изменений и
    правок разбора — прежние улики про другое содержание; `СНИМОК_НЕ_ТРОГАТЬ`
    — ключа `geo_prev` в наборе нет, что лежит в строке, то и остаётся
    (`blocked` со снимком от обхода: воркер при бане снимок не пишет и не
    стирает, и ручка смены провайдера стирать его не вправе — иначе следующий
    слепой суд запишет слабее без удержания). В остальных случаях ключ есть
    всегда: `None` стирает старый снимок.
    `след` — `trace` после сброса (пакет 6.0а: прежний суд — в `trace.prev`,
    `geocode.trace_after_reset(row.trace)`), чтобы аудит после пересуда не
    читал прошлый `rule` как нынешний; путь со строкой в руках (`сбросить_вердикт`,
    обход, CLI) передаёт его, bulk без строки — оставляет след как есть."""
    return {
        "geo_status": geocode.GEO_PENDING,
        "geo_variants": None,
        "geo_lat": None,
        "geo_lon": None,
        "geo_formatted": None,
        "geo_provider": None,
        "geo_checked_at": None,
        "geo_verdict_version": None,
        "geo_without_dadata": False,
        **({} if улики is СНИМОК_НЕ_ТРОГАТЬ else {"geo_prev": улики}),
        **({} if след is СЛЕД_НЕ_ТРОГАТЬ else {"trace": след}),
        **({} if с_попытками else {"geo_attempts": 0}),
    }


def сбросить_вердикт(
    row: ClientAddressCandidate, *, reason: str, улики: dict[str, Any] | None = None
) -> None:
    """Строка меняется — прежний вердикт карты с ней не едет (ревью 13.09):
    статус `pending`, попытки с нуля, ни координат, ни вариантов, ни времени
    проверки — иначе строка с восемью попытками так и осталась бы без карты,
    а старые координаты лежали бы под новым «проверяем». `reason` — причина
    повтора в журнал `geocode.requeue` (единая точка grep по всем путям сброса).
    `geo_prev` помечается изменённым явно: `None` поверх загруженного `NULL`
    SQLAlchemy в UPDATE не кладёт, а снимок мог появиться после загрузки строки
    (обход между чтением и записью живого пути, ревью 20.09)."""
    было = row.geo_status
    след = geocode.trace_after_reset(row.trace if isinstance(row.trace, dict) else None)
    for k, v in сброс_вердикта(с_попытками=False, улики=улики, след=след).items():
        setattr(row, k, v)
    flag_modified(row, "geo_prev")
    flag_modified(row, "trace")
    log.info(
        "geocode.requeue",
        reason=reason,
        candidate_id=str(row.id),
        was=было,
        kept_evidence=улики is not None,
    )


def _к_utc(dt: datetime) -> datetime:
    """SQLite отдаёт наивное время — сравниваем в одном поясе (как `inbound._aware_utc`)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def упоминание_не_старше(message_at: datetime | None, строка: ClientAddressCandidate) -> bool:
    """Сторож порядка (N29, 19.09): части дописываются и строка переезжает в другой
    диалог только от упоминания НЕ СТАРШЕ того, к которому строка привязана
    (`message_at`). Догон истории и сверка приносят реплики задним числом; без
    сторожа старый диалог утаскивал бы к себе живую строку («к последнему
    упоминанию» стало бы «к самому раннему»), а «кв 3» месячной давности перебивала
    бы вчерашнюю «кв 7» — тот же класс, что ревью 14.09 ловило у `address-reparse`.
    Времени нет у одной из сторон (строки до 11.09 без `message_at`, запись без
    сообщения) — упоминание считается новым: поведение до 19.09."""
    if message_at is None or строка.message_at is None:
        return True
    return _к_utc(message_at) >= _к_utc(строка.message_at)


async def record_address_candidate(
    db: AsyncSession,
    *,
    client: Client,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    message_at: datetime | None,
    found: address_parse.Found,
    source: str = CANDIDATE_SOURCE_INBOUND,
    now: datetime,
    overwrite_parts: bool = True,
) -> RecordedAddress:
    """Запомнить распознанный адрес: ``(впервые, id строки, перепроверить)``.

    `overwrite_parts=False` — части (квартира, подъезд…) существующей строки
    не трогать: догон по истории не должен старым значением перебивать то,
    что живой путь уже применил в верном порядке.

    `перепроверить` — строка новая или в ней появился посёлок/город клиента,
    то есть вердикт карты надо считать заново. Пункт от реплики СТАРШЕ строки
    (догон истории) и у строки, решённой человеком, только дописывается: вердикт
    не сбрасывается и перепроверки нет (ревью 19.09).

    ⚠ ИДЕНТИФИКАТОР ВОЗВРАЩАЕТСЯ ЗАТЕМ, ЧТОБЫ ПОСТАВИТЬ ПРОВЕРКУ ПО КАРТЕ — И
    СТАВИТЬ ЕЁ ОБЯЗАН ВЫЗЫВАЮЩИЙ, ПОСЛЕ COMMIT'А. Эта функция живёт внутри
    транзакции; задача, поставленная отсюда, прибежала бы к строке, которой в
    базе ещё нет (класс «кадр до commit'а»).

    ⚠ ЧАСТИ ДОПИСЫВАЮТСЯ В ТУ ЖЕ СТРОКУ, И РАДИ ЭТОГО ВЫБРАН КЛЮЧ БЕЗ КВАРТИРЫ.
    Замер боя: в 23,5 % адресных диалогов адрес размазан по нескольким репликам
    («Ленина 5» → «кв 3, второй подъезд»). Каждый обрывок отдельной строкой
    означал бы три вопроса оператору вместо одного, а очередь предложений в
    этом проекте уже показала свою пропускную способность: у телефона 5 534
    предложения за 30 дней и 24 решения.

    ⚠ ПОЗДНЕЕ ЗНАЧЕНИЕ ЧАСТИ ПОБЕЖДАЕТ РАННЕЕ. Человек, повторяющий квартиру,
    почти всегда либо уточняет, либо ПОПРАВЛЯЕТ себя. Оставив первое, карточка
    показывала бы устаревшее, а поправку не показывала бы вовсе: цитата в
    строке одна.

    ⚠ А ВОТ ЦИТАТА И ССЫЛКА НА СООБЩЕНИЕ НЕ МЕНЯЮТСЯ НИКОГДА. `raw` — это
    доказательство самого адреса, то есть кусок того сообщения, где названы
    улица и дом. Сообщение «кв 7» такой цитатой быть не может: в нём нет ни
    улицы, ни дома, и сторож цитаты на нём не сошёлся бы.
    """
    # ОДИН АДРЕС — ОДНА СТРОКА (бой 13.09): «ул. Некрасова 6» и «Некрасова 6»,
    # «Переулок…» и «переулок…», «сектор Улица Лесная 12» и «Лесная
    # 12» — ключи разные, место одно. Такая строка уже есть — дописываем её,
    # а не заводим вторую; отклонённую не трогаем (оператор сказал «не адрес»).
    ключ = found.value
    if found.kind == address_parse.KIND_HOUSE and found.house:
        такая = await _та_же_строка(db, client_id=client.id, found=found)
        if такая is not None:
            ключ = такая.value
    insert = dialect.insert(db)
    новый_id = uuid.uuid4()
    result = await db.execute(
        insert(ClientAddressCandidate)
        .values(
            id=новый_id,
            client_id=client.id,
            conversation_id=conversation_id,
            message_id=message_id,
            message_at=message_at,
            value=ключ,
            street=found.street,
            house=found.house,
            office=found.parts.get("office"),
            entrance=found.parts.get("entrance"),
            floor=found.parts.get("floor"),
            intercom=found.parts.get("intercom"),
            raw=found.raw,
            level=found.level,
            source=source,
            status=CANDIDATE_PENDING,
            detected_at=now,
            settlement=found.settlement,
            settlement_type=found.settlement_type,
            locality=found.locality,
            kind=found.kind,
            district=found.district,
            area=found.area,
            region=found.region,
            # След разбора (пакет 6.0а): чем и почему так разобрано; ключи
            # суда допишет воркер поверх, не стирая этих.
            trace=след_разбора(found),
            geo_status=geocode.GEO_PENDING,
        )
        .on_conflict_do_nothing(index_elements=["client_id", "value"])
    )
    впервые = bool(getattr(result, "rowcount", 0) == 1)
    if впервые:
        return RecordedAddress(True, новый_id, True)
    строка = (
        await db.execute(
            sa.select(ClientAddressCandidate).where(
                ClientAddressCandidate.client_id == client.id,
                ClientAddressCandidate.value == ключ,
            )
        )
    ).scalar_one_or_none()
    не_старше = строка is not None and упоминание_не_старше(message_at, строка)
    if overwrite_parts and не_старше and строка is not None and _дописать_части(строка, found):
        # «Ул Ленина 5 кв 7» после автозаписи: квартира дописана в строку —
        # и адрес карточки обязан её показать (владелец 18.09, Хабаровск).
        await refresh_auto_address(db, client, строка)
    перепроверить = False
    if строка is not None and строка.status != CANDIDATE_REJECTED:
        # ⚠ ПОСЁЛОК, НАЗВАННЫЙ ПОЗЖЕ, МЕНЯЕТ ВЕРДИКТ. «Ленина 5» → карта
        # подтвердила первую из четырёх; следом «это посёлок Ударник» — и
        # строка обязана перепровериться, а не молчать. В карточку это само
        # ничего не пишет: автозапись идёт только в пустое поле.
        #
        # ⚠ НО НЕ ОТ СТАРОЙ РЕПЛИКИ И НЕ У РЕШЁННОЙ ЧЕЛОВЕКОМ (ревью 19.09).
        # Догон истории и `backfill-cards` приносят реплику СТАРШЕ строки; строку
        # с `exact`, выбранным оператором (`resolved_by_id`), сброс вернул бы к
        # карте и стёр координаты, а вопрос уже решён. Пункт и город при этом
        # дописываются — они только заполняют пустое и не меняют ни привязку,
        # ни части (проект N29 §2.4); сброс и перепроверка — тем же сторожем,
        # что у `refine_address_settlement`.
        вердикт_подвижен = не_старше and строка.resolved_by_id is None
        if found.settlement and not строка.settlement:
            строка.settlement = found.settlement
            строка.settlement_type = found.settlement_type
            if вердикт_подвижен:
                сбросить_вердикт(строка, reason="settlement_named")
                перепроверить = True
        if found.locality and not строка.locality:
            строка.locality = found.locality
            if вердикт_подвижен:
                сбросить_вердикт(строка, reason="locality_named")
                перепроверить = True
        if conversation_id != строка.conversation_id and message_id is not None and не_старше:
            # Адрес назван снова в ДРУГОМ диалоге (ревью 13.09): «дом N» и «кв 5»
            # следом ищут строку в своём диалоге — переносим её к последнему
            # упоминанию, иначе дом и квартира теряются молча. Только вперёд по
            # времени — `упоминание_не_старше` (N29).
            строка.conversation_id = conversation_id
            строка.message_id = message_id
            строка.message_at = message_at
            строка.detected_at = now
            # Источник едет за ссылкой (ревью 19.09): `source` говорит, КАК
            # прочитана реплика по `message_id`, и сторожа речи (`_отсеять_речь`
            # в автозаписи, догон `address-reparse`) по нему решают, перечитывать
            # ли её правилами. Строка правил, перенесённая на повтор, который
            # прочла модель («Ленина пять»), со старым `inbound` судилась бы
            # правилами — и настоящий адрес шёл бы в отклонённые как речь.
            # Одна пара (ссылка, источник) — один источник истины: в обратную
            # сторону (повтор текстом после модели) источник становится `inbound`,
            # и правила на этой реплике адрес видят.
            строка.source = source
        if (
            вердикт_подвижен
            and found.value != строка.value
            and found.kind == address_parse.KIND_HOUSE
        ):
            # То же место, но с типом улицы, которого не было («проспект Ленина 5»
            # после «Ленина 5»): тип уточняет улицу — строка получает его и
            # перепроверяется картой (ревью 13.09). Ключ меняется, только если
            # свободен (отклонённая строка с таким ключом его держит). Старой
            # реплике и решённой человеком строке ключ не меняем: без сброса
            # вердикта новая улица под старыми координатами — ложь на экране.
            if address_parse.street_type(found.street) and not address_parse.street_type(
                строка.street
            ):
                занят = (
                    await db.execute(
                        sa.select(sa.func.count())
                        .select_from(ClientAddressCandidate)
                        .where(
                            ClientAddressCandidate.client_id == client.id,
                            ClientAddressCandidate.value == found.value,
                        )
                    )
                ).scalar_one()
                if not занят:
                    строка.street, строка.value = found.street, found.value
                    сбросить_вердикт(строка, reason="street_type_named")
                    перепроверить = True
        if _уровень_выше(found.level, строка.level):
            # «Некрасова 6» (C) → «ул. Некрасова 6» (A): уверенная реплика поднимает
            # уровень строки, иначе предложение остаётся скрытым (ревью 13.09).
            строка.level = found.level
            # Уровень теперь от этой реплики — и след разбора от неё же;
            # ключи суда в `trace` не трогаются.
            новый_след = след_разбора(found)
            if новый_след:
                строка.trace = {**(строка.trace or {}), **новый_след}
    return RecordedAddress(False, (строка.id if строка is not None else None), перепроверить)


#: Ключи `trace`, которые пишет слой разбора (`Found`), — контракт с пакетом 7.
_КЛЮЧИ_СЛЕДА_РАЗБОРА: tuple[str, ...] = (
    "parse_form",
    "level_reason",
    "locality_form",
    "settlement_read",
)


def след_разбора(found: address_parse.Found) -> dict[str, Any] | None:
    """След разбора для `trace` строки: только непустые ключи; None — следа
    нет (в колонку ложится SQL NULL, а не пустой объект). Прочие ключи слоя
    разбора (`Found.trace`: `parse_shadow`, `stop_shadow`, `form`) — тем же
    словарём, именованные ключи поверх них."""
    след = {**(found.trace or {}), **{k: getattr(found, k, None) for k in _КЛЮЧИ_СЛЕДА_РАЗБОРА}}
    return {k: v for k, v in след.items() if v} or None


def _уровень_выше(новый: str, старый: str) -> bool:
    порядок = {"A": 3, "B": 2, "C": 1}
    return порядок.get(новый, 0) > порядок.get(старый, 0)


async def _части_места(
    db: AsyncSession, candidate: ClientAddressCandidate
) -> dict[str, str | None]:
    """Квартира/подъезд/этаж/домофон по всем не отклонённым строкам того же
    места у клиента — позднее значение побеждает раннее."""
    строки = (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == candidate.client_id,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
            )
            .order_by(ClientAddressCandidate.detected_at)
        )
    ).scalars()
    части: dict[str, str | None] = dict.fromkeys(_ЧАСТИ_АДРЕСА)
    for r in [*строки, candidate]:
        # Той же семьёй, что и показ (`_без_дублей`): запись обязана совпасть
        # с показом (ревью 13.09, 15.09).
        if r.id != candidate.id and not одно_место(r, candidate):
            continue
        for ч in _ЧАСТИ_АДРЕСА:
            if getattr(r, ч) is not None:
                части[ч] = getattr(r, ч)
    return части


async def _та_же_строка(
    db: AsyncSession, *, client_id: uuid.UUID, found: address_parse.Found
) -> ClientAddressCandidate | None:
    """Не отклонённая строка того же клиента с тем же адресом по смыслу.

    Из нескольких — та же буква в букву, затем принятая (в карточке), затем
    подтверждённая картой, затем ранняя: части должны лечь в строку, которую
    видит оператор, а не в скрытый дубль (ревью 13.09).
    """
    строки = [
        r
        for r in (
            await db.execute(
                sa.select(ClientAddressCandidate)
                .where(
                    ClientAddressCandidate.client_id == client_id,
                    ClientAddressCandidate.kind == address_parse.KIND_HOUSE,
                    ClientAddressCandidate.status != CANDIDATE_REJECTED,
                )
                .order_by(ClientAddressCandidate.detected_at)
            )
        ).scalars()
        if address_parse.same_address(r.street, r.house, found.street, found.house)
    ]
    if not строки:
        return None
    return max(
        строки,
        key=lambda r: (
            r.value == found.value,
            r.status == CANDIDATE_ACCEPTED,
            r.geo_status == geocode.GEO_EXACT,
            -r.detected_at.timestamp(),
        ),
    )


def _дописать_части(строка: ClientAddressCandidate, found: address_parse.Found) -> bool:
    """Дописать в существующую строку части, названные позже. ``True`` — дописали.

    Обновляются только те, что пришли в этом сообщении: пустое значение не
    затирает уже известное. «Ленина 5» без квартиры не обязано стирать
    квартиру, названную вчера. Отклонённую строку не трогаем: оператор сказал
    «это не адрес», и дописывать в неё подробности значит спорить с ним молча.
    """
    новые = {ч: found.parts[ч] for ч in _ЧАСТИ_АДРЕСА if found.parts.get(ч)}
    if not новые or строка.status == CANDIDATE_REJECTED:
        return False
    for ч, значение in новые.items():
        setattr(строка, ч, значение)
    return True


def candidate_grade(row: ClientAddressCandidate) -> str | None:
    """Степень строки для карточки — ЕДИНСТВЕННЫЙ предикат автозаписи в этом
    модуле и в воркере (владелец 18.09: «адрес привязывается автоматически
    без участия человека»). Читают: `autofill_address` (фильтр, память об
    отказе, выбор из нескольких), `_по_местам`, `auto_address_yields_to`,
    постановка автозаписи. Судья один — `geocode.card_grade` (контракт 18.09
    п.3); здесь только распаковка строки, своего предиката нет.

    * `exact` — карта подтвердила дом и дала его точку;
    * `approx` — точка есть, но улицы/массива/пункта (`~approx` у провайдера)
      или это место (`kind=place`);
    * `text` — карта окончательно отказала, но улицу клиента в его пункте
      знает: воркер собрал текст без точки в `geo_formatted`;
    * `None` — не годна: ещё проверяется, отказ без текста, `exact` без
      координат (инвариант нарушен).
    """
    return geocode.card_grade(
        row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
    )


def candidate_precision(row: ClientAddressCandidate) -> str:
    """Степень точки для экрана и журнала — `exact` / `approx` / `none`."""
    return geocode.point_precision(
        row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon
    )


def candidate_address_text(row: ClientAddressCandidate, *, parts: dict[str, str | None]) -> str:
    """Текст карточки из строки — ЕДИНСТВЕННЫЙ вход к `geocode.address_text`
    у кнопки, пересборки и автозаписи. Ворота «есть ли строка карты» — степень
    (`candidate_grade`, контракт 18.09 п.4): с точкой (`exact`/`approx`) или
    текст без точки — в поле идёт `geo_formatted`; без степени (проверяется,
    отказ без текста, `exact` без координат — инвариант нарушен) — слова
    клиента «улица, дом» и названный им пункт (`_слова_клиента`). Сборка
    частей и района — в `geocode.address_text`.
    """
    есть_степень = candidate_grade(row) is not None
    return geocode.address_text(
        geo_formatted=row.geo_formatted if есть_степень else None,
        geo_status=row.geo_status,
        value=row.value if есть_степень else _слова_клиента(row),
        parts=parts,
        district=row.district,
        area=row.area if row.kind == address_parse.KIND_HOUSE else None,
    )


def _слова_клиента(row: ClientAddressCandidate) -> str:
    """«Улица, дом» и пункт (или город) словами клиента — основа текста
    карточки, когда строки карты нет.

    Ревью 19.09: «Садовая 3 кв 21» лёг автозаписью «ул Садовая, 3,
    Саранск, кв 21», следом «Ялга!» — пункт дописан, вердикт сброшен, карта
    окончательно отказала — и пересборка собирала карточку из `value`: «ул
    Садовая, 3, кв 21» — без города и без того самого пункта, из-за
    которого вердикт сбросили. Пункт — с типом, как назвал клиент («посёлок
    Ялга»); город клиента (`locality`) — когда пункта нет.
    """
    основа = row.value
    пункт = row.settlement or row.locality
    if not пункт or geocode._норм(пункт) in geocode._норм(основа):
        return основа
    тип = row.settlement_type if row.settlement else None
    return f"{основа}, {тип} {пункт}" if тип else f"{основа}, {пункт}"


def grade_weight(степень: str | None) -> int:
    """Вес степени для сравнений и сортировок: чем БОЛЬШЕ, тем выше по
    лестнице text → approx → exact; `None` (не годна) — ниже всех.

    ЕДИНСТВЕННОЕ место, где читается направление `geocode.GRADE_RANK`
    (у участка A меньше — лучше): лестница уступок, память об отказе, выбор
    из нескольких и `_по_местам` сравнивают только через `grade_weight` /
    `grade_beats`, иначе знак ранга разошёлся бы по четырём местам.
    """
    if степень is None:
        return 0
    return len(geocode.GRADE_RANK) - geocode.GRADE_RANK[степень]


def grade_beats(новая: str | None, прежняя: str | None) -> bool:
    """Строго выше по лестнице: text → approx → exact; любая степень выше
    `None`; равные не бьют друг друга."""
    return grade_weight(новая) > grade_weight(прежняя)


async def refresh_auto_address(
    db: AsyncSession, client: Client, row: ClientAddressCandidate
) -> bool:
    """Пересобрать адрес, записанный в карточку автоматикой, из его строки —
    после того как в неё (или в строку той же семьи) дописали части.
    ``True`` — текст карточки изменился.

    Владелец 18.09 (Хабаровск): «Лесная 7а» легло автозаписью «ул Лесная, 7а,
    Хабаровск», следом «2 подъезд, квартира 41.» — части дописались в строку и
    видны в цитате предложения, а строка адреса в карточке так и осталась без
    них: текст собирается один раз при записи (`apply_candidate_to_card`), и
    дописывание частей его не трогало. Мастер едет по карточке, а не по
    цитате — квартира обязана быть в адресе, как у «ул Садовая, 3,
    Саранск, кв 21, подъезд 3, этаж 2», где части были в той же реплике.

    ⚠ ТОЛЬКО АДРЕС АВТОМАТИКИ. Набранный руками (`address_set_at`) и
    подтверждённый кнопкой (`resolved_by_id` у источника) — слова человека:
    он видел цитату и решил, что в поле стоит именно это. Текст — тем же
    сборщиком, что у записи, и с частями всей семьи (`_части_места`): запись
    обязана совпасть с показом (ревью 13.09).
    """
    if (
        client.address is None
        or client.address_set_at is not None
        or client.address_candidate_id is None
    ):
        return False
    источник = (
        row
        if row.id == client.address_candidate_id
        else await db.get(ClientAddressCandidate, client.address_candidate_id)
    )
    if источник is None or источник.resolved_by_id is not None:
        return False
    if источник.id != row.id and not одно_место(row, источник):
        return False
    if источник.geo_status in geocode.CHECKING_STATUSES or geocode.point_is_suggest(
        источник.geo_provider
    ):
        # Вердикт источника сброшен (дописан пункт) или карта ещё не ответила —
        # строка проверяется; в поле лежит прежний текст, трогать рано: части
        # доедут с новым вердиктом (`workers/geocode._пометить`). Ворота — по
        # вердикту, а не по степени: источник уже в карточке, и вопрос только
        # «какой текст его представляет» — строку карты при степени, слова
        # клиента при окончательном отказе без текста (18.09).
        #
        # Предложение (`~suggest`) — состояние «не трогать» (ревью 20.09, #2):
        # пересуд записанного источника под политикой `suggest` выносит ТО ЖЕ
        # решение с хвостом, `candidate_grade` → None, и без этих ворот в поле
        # ложились бы слова клиента с причиной `verdict_refused` — при том, что
        # карта не отказывала. В поле остаётся прежний текст; снять или принять
        # предложение может только человек (`resolve_address_candidate`,
        # «Адрес неверный») или `address-unfill`. Сторож здесь, а не в
        # `_пометить`: закрывает оба входа — воркер и `record_address_candidate`
        # при дописанных частях.
        return False
    текст = candidate_address_text(источник, parts=await _части_места(db, источник))
    if текст == client.address:
        return False
    previous = client.address
    client.address = текст
    await write_audit(
        db,
        user_id=None,
        action="client.address_edited",
        entity="client",
        entity_id=str(client.id),
        details={
            "source": "regex",
            # Карта окончательно отказала строке без текста — в поле легли
            # слова клиента с пунктом (`_слова_клиента`), а не дописанные
            # части: журнал называет причину своим словом (ревью 19.09).
            "reason": "parts_refined"
            if candidate_grade(источник) is not None
            else "verdict_refused",
            "previous": previous,
            "address": текст,
            "conversation_id": str(row.conversation_id),
            "candidate_id": str(источник.id),
            "geo_status": источник.geo_status,
            "precision": candidate_precision(источник),
        },
    )
    return True


#: Причины, по которым адрес автоматики уступает новой годной строке
#: (:func:`auto_address_yields_to`) — слова для лога автозаписи.
YIELD_PLACE_TO_HOUSE = "place_to_house"
#: Поправка того же разбора: другое место из той же реплики, не хуже по степени.
YIELD_SAME_REPLY = "same_reply_reparsed"
#: То же место, строго выше по лестнице: text → approx → exact.
YIELD_GRADE_UP = "grade_up"
#: «29» → «29 А» той же семьи, обе с точкой дома (замер 18.09: 8/60 exact).
YIELD_FAMILY_REFINED = "family_refined"


def _уточнение_семьи(новая: ClientAddressCandidate, прежняя: ClientAddressCandidate) -> bool:
    """«29» → «29 А», «29 к 1» той же семьи: ключ нового дома продолжает ключ
    прежнего. «29 к 1» при «29 к 2» — не уточнение, а спор двух корпусов."""
    ключ_новой, ключ_прежней = geocode.house_key(новая.house), geocode.house_key(прежняя.house)
    return (
        _одна_семья_дома(новая, прежняя)
        and ключ_новой.startswith(ключ_прежней)
        and len(ключ_новой) > len(ключ_прежней)
    )


def _другой_пункт(a: ClientAddressCandidate, b: ClientAddressCandidate) -> bool:
    """Обе строки называют пункт (или город клиента) — и разный. Названный у
    одной — не спор: дом без пункта после «место; дом» несёт пункт места."""
    for поле in ("settlement", "locality"):
        x, y = geocode._норм(getattr(a, поле)), geocode._норм(getattr(b, поле))
        if x and y and x != y:
            return True
    return False


async def auto_address_yields_to(
    db: AsyncSession, client: Client, новая: ClientAddressCandidate
) -> tuple[ClientAddressCandidate | None, str | None]:
    """Уступает ли адрес карточки, записанный автоматикой, новой годной
    строке (`candidate_grade`). Возвращает ``(прежняя строка-источник,
    причина)``; причина ``None`` — не уступает (или карточка пуста — тогда и
    уступать нечему, автозапись пишет в пустое поле обычным порядком).

    Правило целиком (владелец 18.09: «правки руками автоматика не трогает
    никогда», лестница уступок text → approx → exact):

    * набранное руками (`address_set_at`) и подтверждённое кнопкой
      (`resolved_by_id` у источника) — не уступает никогда;
    * место (`place`) уступает дому: «деревня Ивановка» → «ул Лесная 3» —
      тот же адрес, точнее (13.09); но не дому, который сам называет ДРУГОЙ
      пункт или город («деревня Ивановка» → «д Петровка, ул Лесная 3»,
      ревью 19.09) — это два места, а между местами карточка автоматикой не
      переезжает; дом без пункта (после `_место_к_дому` он несёт пункт места)
      уступку сохраняет;
    * проверяемое (`CHECKING_STATUSES`: вердикт сброшен, карта не ответила)
      — не уступает: это не отказ, а ожидание;
    * поправка того же разбора — другое место из ТОЙ ЖЕ реплики (или с одной
      цитатой), не хуже по степени (Ангарск: «85-й квартал, 17 / улица
      Гагрина, 12» — старый разбор дал «ул Гагарина, 17», новый — «кв-л 85,
      17»; автоматика обязана исправлять СВОЮ ошибку сама);
    * внутри ОДНОГО места выше по степени — уступает (текст «Ленина 5» →
      точный дом той же семьи); равная точная степень — только уточнению
      семьи («29» → «29 А»); две карты об одном доме не спорят, карточка не
      дёргается; понижения нет ни по одной ветке;
    * два РАЗНЫХ места из разных реплик — никогда: законно два адреса (свой
      и мамин), карточка автоматикой между местами не переезжает, вторая
      строка показывается «Также назван»; кто ложится в ПУСТУЮ карточку —
      решает `workers/geocode._выбрать_из_нескольких`.

    Межместный ответ на вопрос об адресе поверх уже заполненной карточки
    намеренно НЕ реализован — открытый вопрос владельцу (проект 18.09).
    """
    if (
        client.address is None
        or client.address_set_at is not None  # руками — никогда
        or client.address_candidate_id is None
        or новая.kind != address_parse.KIND_HOUSE
    ):
        return None, None
    новая_степень = candidate_grade(новая)
    if новая_степень is None:
        return None, None
    прежняя = await db.get(ClientAddressCandidate, client.address_candidate_id)
    if прежняя is None or прежняя.id == новая.id:
        return None, None
    if прежняя.kind == address_parse.KIND_PLACE:
        if _другой_пункт(новая, прежняя):
            return None, None
        return прежняя, YIELD_PLACE_TO_HOUSE
    if прежняя.resolved_by_id is not None:  # подтверждено кнопкой — слова человека
        return None, None
    if прежняя.geo_status in geocode.CHECKING_STATUSES:  # ещё проверяется — не отказ
        return None, None
    # `None` — окончательный отказ без текста: любая степень выше него.
    прежняя_степень = candidate_grade(прежняя)
    не_хуже = not grade_beats(прежняя_степень, новая_степень)
    та_же_реплика = (прежняя.message_id is not None and прежняя.message_id == новая.message_id) or (
        bool(прежняя.raw) and прежняя.raw == новая.raw
    )
    одно = одно_место(новая, прежняя)
    if та_же_реплика and not одно and не_хуже:
        return прежняя, YIELD_SAME_REPLY
    if not одно:
        return None, None
    if grade_beats(новая_степень, прежняя_степень):
        return прежняя, YIELD_GRADE_UP
    if новая_степень == прежняя_степень == geocode.GRADE_EXACT and _уточнение_семьи(новая, прежняя):
        return прежняя, YIELD_FAMILY_REFINED
    return None, None


async def refine_address_parts(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    parts: dict[str, str],
    before: datetime | None = None,
) -> bool:
    """Дописать части в адрес, уже найденный В ЭТОМ ЖЕ диалоге. ``True`` — дописали.

    ⚠ ТОЛЬКО В СВОЙ ДИАЛОГ, И ЭТО ГЛАВНОЕ ОГРАНИЧЕНИЕ. «Кв 3» из переписки про
    другой заказ не имеет права дописаться в адрес, названный месяц назад в
    другом диалоге: у человека законно два адреса (свой и мамин), и склеить их
    квартирой значило бы отправить мастера по половине одного и половине
    другого.

    ⚠ И ТОЛЬКО В ПОСЛЕДНИЙ. Если в одном диалоге названы два адреса, части
    относятся к тому, о котором говорили только что.

    Отклонённые строки не трогаем: оператор сказал «это не адрес», и дописывать
    в них подробности значит спорить с ним молча.
    """
    if not parts:
        return False
    строка = (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == client_id,
                ClientAddressCandidate.conversation_id == conversation_id,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
                *_не_позже(before),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if строка is None:
        return False
    for имя, значение in parts.items():
        setattr(строка, имя, значение)
    # Адрес автоматики в карточке собран из этой строки — квартира и подъезд
    # обязаны попасть и в него, а не только в цитату (владелец 18.09).
    client = await db.get(Client, client_id)
    if client is not None:
        await refresh_auto_address(db, client, строка)
    return True


async def refine_address_settlement(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    settlement: str | None,
    settlement_type: str | None,
    quote: str,
    since: datetime,
    before: datetime | None = None,
    locality: str | None = None,
    kind: str | None = None,
    only_statuses: frozenset[str | None] | None = None,
) -> ClientAddressCandidate | None:
    """Пункт (или город клиента — `locality`), названный ПОСЛЕ улицы, — в
    последнюю строку этого диалога без пункта. Возвращает строку, если
    дописали: её надо перепроверить картой.

    `kind` — фильтр WHERE: только строки этого вида (N13: нужна последняя
    строка ДОМА — только что вставленная строка места иначе была бы
    «последней» и отсеялась по пустой улице). `only_statuses` — проверка
    ПОСЛЕ выбора последней строки, а не в WHERE: иначе фильтр выбрал бы более
    старую неспокойную строку («Мира 7») вместо последней («Ленина 5» exact);
    пункт относится к последнему адресу или ни к какому.

    Владелец 18.09 («определение адресов должно работать с контекстом»):
    «Садовая 3 кв 21» → «Ялга!» — без Ялги карта подтвердила Садовая 3
    в самом Саранске, а дом — в рабочем посёлке Ялга. Пункт из следующей
    реплики дописывается туда же, куда «кв 3» (`refine_address_parts`): только
    в свой диалог, только в последнюю строку, только за сутки (`since`) — и
    только в строку, у которой пункта, массива и города ещё нет: названный
    пункт не спорит с названным, спорить — дело оператора.

    ⚠ ЦИТАТА ДОПОЛНЯЕТСЯ, А НЕ ПОДМЕНЯЕТСЯ: «Ялга; ул Садовая д 3» — пункт
    впереди, как у склейки «место; дом» (`_место_к_дому`): сторож цитаты и
    «дом N» к склеенной строке читают пункт по первому куску. Слова клиента
    в цитате остаются его словами — оператор видит, откуда взялся пункт.
    Вердикт карты сбрасывается: считался без пункта.

    ⚠ РЕШЁННУЮ ЧЕЛОВЕКОМ СТРОКУ НЕ ТРОГАЕМ (ревью 18.09): подтверждённое
    кнопкой (`resolved_by_id`) — слова человека, сбрасывать его вердикт и
    отправлять строку к карте заново автоматика не вправе; спорить с
    клиентом о пункте — дело оператора, он в диалоге.
    """
    строка = (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == client_id,
                ClientAddressCandidate.conversation_id == conversation_id,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
                ClientAddressCandidate.detected_at >= since,
                *([ClientAddressCandidate.kind == kind] if kind is not None else []),
                *_не_позже(before),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if строка is None or строка.resolved_by_id is not None:
        return None
    if only_statuses is not None and строка.geo_status not in only_statuses:
        return None
    if строка.settlement or строка.area or строка.locality:
        return None
    if not (строка.street or "").strip() or not (settlement or locality):
        return None
    if settlement:
        строка.settlement = settlement
        строка.settlement_type = settlement_type
    else:
        строка.locality = locality
    if quote and quote not in (строка.raw or ""):
        строка.raw = f"{quote}; {строка.raw}"
    сбросить_вердикт(строка, reason="late_place")
    return строка


async def refine_address_area(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    area: str,
    quote: str,
    since: datetime,
    before: datetime | None = None,
) -> ClientAddressCandidate | None:
    """Микрорайон/квартал, названный ПОСЛЕ дома, — в последнюю строку дома
    этого диалога без массива (владелец 19.09, Волжский: «пл Мира 11» → «9
    микрорайон»). Внутригородская единица города не меняет, поэтому вердикт
    карты НЕ сбрасывается — дописывается только `area`, и карточка, если её
    держит эта строка, пересобирается текстом (`refresh_auto_address`).
    Решённую человеком строку не трогаем."""
    строка = (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == client_id,
                ClientAddressCandidate.conversation_id == conversation_id,
                ClientAddressCandidate.kind == address_parse.KIND_HOUSE,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
                ClientAddressCandidate.detected_at >= since,
                *_не_позже(before),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if строка is None or строка.resolved_by_id is not None:
        return None
    if строка.area or not (строка.street or "").strip() or not area.strip():
        return None
    строка.area = area.strip()
    if quote and quote not in (строка.raw or ""):
        строка.raw = f"{строка.raw}; {quote}"
    return строка


def _не_позже(before: datetime | None) -> list[Any]:
    """Верхняя граница по времени обнаружения — для догона по прожитой
    переписке (ревью 15.09): живой путь строк «из будущего» не видит, а догон,
    переигрывая историю, видел бы и клеил «дом 9» к месту, названному позже."""
    return [ClientAddressCandidate.detected_at <= before] if before is not None else []


async def latest_address_candidate(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    since: datetime,
    before: datetime | None = None,
) -> ClientAddressCandidate | None:
    """Последняя не отклонённая строка адреса ЭТОГО диалога не старше `since`
    (и не моложе `before`, если дана)."""
    return (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == client_id,
                ClientAddressCandidate.conversation_id == conversation_id,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
                ClientAddressCandidate.detected_at >= since,
                *_не_позже(before),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_house_candidate(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    since: datetime,
    before: datetime | None = None,
) -> ClientAddressCandidate | None:
    """Последняя не отклонённая строка-дом (с улицей) этого диалога не старше `since`."""
    return (
        await db.execute(
            sa.select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.client_id == client_id,
                ClientAddressCandidate.conversation_id == conversation_id,
                ClientAddressCandidate.status != CANDIDATE_REJECTED,
                ClientAddressCandidate.kind == address_parse.KIND_HOUSE,
                ClientAddressCandidate.street != "",
                ClientAddressCandidate.detected_at >= since,
                *_не_позже(before),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def address_candidates(
    db: AsyncSession,
    client_id: uuid.UUID,
    *,
    only_pending: bool = False,
    include_merged: bool = True,
) -> list[ClientAddressCandidate]:
    """Распознанные адреса карточки, старые первыми.

    Вместе с адресами присоединённых карточек — по тому же правилу, что у
    телефонов: объединение не переносит строки, а карточка-победитель читает их
    по связи, и разъединение возвращает всё само собой.
    """
    ids = await _группа_карточек(db, client_id, include_merged=include_merged)
    q = sa.select(ClientAddressCandidate).where(ClientAddressCandidate.client_id.in_(ids))
    if only_pending:
        q = q.where(ClientAddressCandidate.status == CANDIDATE_PENDING)
    q = q.order_by(ClientAddressCandidate.detected_at)
    return list((await db.execute(q)).scalars().all())


async def phone_candidates(
    db: AsyncSession,
    client_id: uuid.UUID,
    *,
    only_pending: bool = False,
    include_merged: bool = True,
) -> list[ClientPhoneCandidate]:
    """Распознанные номера карточки, старые первыми.

    ВМЕСТЕ С НОМЕРАМИ ПРИСОЕДИНЁННЫХ КАРТОЧЕК (решение владельца 19.08).
    Объединение больше не переносит кандидатов: они остаются там, где были
    распознаны, а карточка-победитель читает их по связи. Так разъединение
    возвращает всё само собой — двигать и восстанавливать нечего.

    Цепочку идём вглубь: карточка могла быть присоединена к присоединённой.
    Потолок глубины намеренный — кольцо в данных не должно вешать чтение.
    """
    ids = await _группа_карточек(db, client_id, include_merged=include_merged)
    stmt = sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.client_id.in_(ids))
    if only_pending:
        stmt = stmt.where(ClientPhoneCandidate.status == CANDIDATE_PENDING)
    stmt = stmt.order_by(ClientPhoneCandidate.detected_at, ClientPhoneCandidate.id)
    return list((await db.execute(stmt)).scalars().all())


def candidate_view(row: ClientPhoneCandidate) -> dict[str, Any]:
    """Один кандидат для экрана. Состав полей — контракт с интерфейсом.

    `raw` и `message_id` здесь не для полноты: оператор обязан иметь
    возможность сверить нашу догадку с тем, что клиент написал на самом деле, и
    открыть то самое сообщение. Предложение, которое нельзя проверить, либо
    принимают не глядя, либо перестают замечать.
    """
    return {
        "id": str(row.id),
        "phone": row.phone,
        "raw": row.raw,
        "conversation_id": str(row.conversation_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "message_at": row.message_at.isoformat() if row.message_at else None,
        "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        "source": row.source,
        "status": row.status,
    }


#: Что оператор может сделать с распознанным номером. Слова взяты из
#: постановки владельца («заменить», «добавить», «отклонить») намеренно: экран
#: покажет ровно эти три кнопки, и переводить их в другие слова по дороге
#: значило бы завести четвёртое понимание того же действия.
CANDIDATE_DECISIONS = ("replace", "add", "reject")


async def resolve_phone_candidate(
    db: AsyncSession,
    candidate: ClientPhoneCandidate,
    *,
    decision: str,
    actor: User,
    card: Client | None = None,
) -> dict[str, Any]:
    """Решение оператора по распознанному номеру. Транзакцией владеет вызывающий.

    ТРИ ИСХОДА И ЧЕМ ОНИ ОТЛИЧАЮТСЯ.

    * `replace` — «это его основной телефон». Номер встаёт в `clients.phone`.
      ПРЕЖНИЙ ПРИ ЭТОМ ЗАТИРАЕТСЯ, и это стоит знать: он остаётся в карточке
      списком, только если сам когда-то был распознан из переписки и принят
      («добавить»). Номер, набранный руками или принесённый ботом, уедет из
      поля, и единственным его следом будет строка журнала
      `client.phone_edited` с `details.previous`. Так же ведёт себя и обычное
      исправление номера руками (:func:`set_phone`) — заводить здесь другое
      поведение значило бы, что «заменить» кнопкой и «заменить» руками делают
      разное.
    * `add` — «телефон его, но основной другой». Карточка показывает номер в
      списке `identity_view.phones`, `clients.phone` не трогается вовсе. Это и
      есть безопасный ответ для второго номера («звоните жене»).
    * `reject` — «это не телефон» или «это не его телефон». Строка остаётся с
      пометкой: без неё тот же номер из того же сообщения предлагался бы
      заново после каждого пересчёта по истории.

    ПОЧЕМУ РЕШЕНИЕ ПИШЕТСЯ С АВТОРОМ. Через месяц вопрос «кто вписал в карточку
    этот номер» обязан иметь ответ. У автоматической записи автора нет, и это
    видно по пустому `resolved_by_id`; у нажатой кнопки автор есть всегда.
    """
    if decision not in CANDIDATE_DECISIONS:
        raise ApiError(
            "validation_error",
            "Допустимые решения: заменить, добавить, отклонить",
            status=422,
            details={
                "fields": [
                    {
                        "field": "decision",
                        "rule": "enum",
                        "message": "|".join(CANDIDATE_DECISIONS),
                    }
                ]
            },
        )
    # Карточка, открытая у оператора. Номер присоединённой карточки группы
    # лежит строкой у неё, но «Заменить» — про человека на экране: иначе номер
    # уходил в скрытую карточку, а открытая оставалась со старым.
    client = card if card is not None else await db.get(Client, candidate.client_id)
    if client is None:  # pragma: no cover — карточка удалена вместе с кандидатом
        raise ApiError("not_found", "Карточка клиента не найдена", status=404)

    # «НЕ ЕГО НОМЕР» НА УЖЕ ПРИНЯТОМ ДОПОЛНИТЕЛЬНОМ (правка 12.09). Автоматика
    # добавляет номера из переписки без вопроса, и единственная обратимость —
    # человек снимает лишний одним нажатием. Основной так не снять: у него
    # своё поле и своя правка.
    снимаем_принятый = decision == "reject" and candidate.status == CANDIDATE_ACCEPTED
    if снимаем_принятый and candidate.phone == client.phone:
        raise ApiError(
            "is_primary",
            "Это основной номер карточки — исправьте его в поле телефона",
            status=422,
        )
    # И ОБРАТНО: «Вернуть» снятый — это «добавить» на отклонённой строке. Без
    # него нажатие «Не его номер» было бы необратимым: повтор номера клиентом
    # упирается в ON CONFLICT, и вернуть его не смог бы никто.
    возвращаем_снятый = decision == "add" and candidate.status == CANDIDATE_REJECTED
    if candidate.status != CANDIDATE_PENDING and not (снимаем_принятый or возвращаем_снятый):
        raise ApiError(
            "already_resolved",
            "По этому номеру решение уже принято",
            status=409,
        )
    было_автоматикой = candidate.resolved_by_id is None and candidate.status == CANDIDATE_ACCEPTED

    # УСЛОВНАЯ ЗАПИСЬ, А НЕ ПРИСВАИВАНИЕ. Ночная починка старых предложений
    # решает те же строки; проиграть ей нажатием и получить в журнале две
    # противоположные записи об одном номере нельзя.
    ожидали = candidate.status
    result = await db.execute(
        sa.update(ClientPhoneCandidate)
        .where(ClientPhoneCandidate.id == candidate.id, ClientPhoneCandidate.status == ожидали)
        .values(
            status=CANDIDATE_REJECTED if decision == "reject" else CANDIDATE_ACCEPTED,
            resolved_at=datetime.now(UTC),
            resolved_by_id=actor.id,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise ApiError(
            "already_resolved",
            "По этому номеру решение уже принято",
            status=409,
        )
    await db.refresh(candidate, ["status", "resolved_at", "resolved_by_id"])

    if decision == "reject":
        await write_audit(
            db,
            user_id=actor.id,
            action="client.phone_candidate_rejected",
            entity="client",
            entity_id=str(client.id),
            details={
                "phone": candidate.phone,
                "raw": candidate.raw,
                "conversation_id": str(candidate.conversation_id),
                "overrides_auto": было_автоматикой,
            },
        )
        return {"phone": client.phone, "candidate": candidate_view(candidate)}

    if decision == "add":
        await write_audit(
            db,
            user_id=actor.id,
            action="client.phone_candidate_accepted",
            entity="client",
            entity_id=str(client.id),
            details={
                "phone": candidate.phone,
                "conversation_id": str(candidate.conversation_id),
                "primary": False,
            },
        )
        return {"phone": client.phone, "candidate": candidate_view(candidate)}

    previous = client.phone
    client.phone = candidate.phone
    # ДИАЛОГ, КОТОРЫМ НОМЕР ДОКАЗАН. Кандидат родился в конкретной переписке, и это
    # самое сильное доказательство принадлежности, какое у нас есть: номер написан
    # ЭТИМ человеком в ЭТОМ разговоре. Заявка спрашивает именно его
    # (`leads.lead_phone`), чтобы после объединения карточек не уехать в лид-центр с
    # номером из соседнего диалога.
    client.phone_conversation_id = candidate.conversation_id
    # Происхождение номера — канал того диалога, где его написали. Без этого
    # «телефон совпал» ничего не доказывает межканальной проверке
    # (`inbound._apply_phone_evidence`, и там же почему).
    client.phone_account_id = (
        await db.execute(
            sa.select(Conversation.account_id).where(Conversation.id == candidate.conversation_id)
        )
    ).scalar_one_or_none()
    # Руками номер НЕ вводили — его вычитали из переписки, а человек лишь
    # согласился. `phone_set_by_id`/`phone_set_at` означают «набрал с клавиатуры»
    # и подписывают карточку словами «введено вручную»; поставить их здесь
    # значило бы соврать о происхождении цифр.
    client.phone_set_by_id = None
    client.phone_set_at = None
    await write_audit(
        db,
        user_id=actor.id,
        # Первое заполнение и исправление — разные события: метрика «собрано
        # телефонов» (06 §1.4) считает первое, и одна опечатка, поправленная
        # трижды, дала бы «собрано 4 телефона» из одного.
        action="client.phone_captured" if previous is None else "client.phone_edited",
        entity="client",
        entity_id=str(client.id),
        details={
            # `source='regex'` — колонка «из них автоизвлечением» в отчёте.
            # Цифры и правда добыты разбором; оператор их подтвердил, а не
            # набрал, и записать сюда `manual` значило бы приписать человеку
            # чужую работу.
            "source": "regex",
            "phone": candidate.phone,
            "previous": previous,
            "conversation_id": str(candidate.conversation_id),
        },
    )
    return {"phone": client.phone, "candidate": candidate_view(candidate)}


async def make_phone_primary(
    db: AsyncSession,
    client: Client,
    raw: str,
    *,
    actor: User,
    conversation_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Сделать основным один из УЖЕ ИЗВЕСТНЫХ номеров этого человека.

    ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «когда клиент даёт 2 номер, то
    исправить можно только основной номер, второй изменить нельзя. Так же
    нельзя поменять их местами».

    Так и было. Второй номер живёт строкой принятого кандидата, и действий у
    неё не было ни одного: карточка печатала «Ещё номера этого человека: …»
    обычным текстом. Чтобы поменять номера местами, оператору приходилось
    перепечатывать оба руками — и первый при этом ПРОПАДАЛ, потому что
    `set_phone` прежний затирает.

    ⚠ ПРЕЖНИЙ ОСНОВНОЙ СОХРАНЯЕТСЯ, И ЭТО ГЛАВНОЕ ОТЛИЧИЕ ОТ РУЧНОЙ ПРАВКИ.
    Обмен — не исправление опечатки, а перестановка: оба номера принадлежат
    человеку, и потерять один значит сделать ровно то, от чего оператор
    защищался. Поэтому старый уходит в принятые кандидаты с источником `swap`.

    Ручная правка (:func:`set_phone`) прежний по-прежнему затирает, и это не
    непоследовательность: там человек ИСПРАВЛЯЕТ неверный номер, и оставить
    неверное вторым значило бы показывать в карточке заведомую ложь, по
    которой однажды позвонят.

    ⚠ ТОЛЬКО ИЗ ИЗВЕСТНЫХ. Произвольная строка сюда не принимается: для этого
    есть `set_phone` со своим разбором, своим журналом и своей метрикой. Иначе
    действие «сделать основным» стало бы вторым способом вписать любой номер, а
    два пути к одному полю в этом проекте расходятся с завидным постоянством.

    ⚠ ИЗВЕСТНЫМ СЧИТАЕТСЯ ТОЛЬКО ПРИНЯТЫЙ КАНДИДАТ ЭТОЙ КАРТОЧКИ, И ЭТО СУЖЕНИЕ
    ПОСЛЕ РАЗБОРА. Первая редакция принимала и номера ПРИСОЕДИНЁННЫХ карточек —
    они ведь тоже показаны списком. Разбор показал цену: обмен становился
    однокликовым способом перенести номер через границу карточек, а
    «Разъединить» его назад не забирает — у победителя оставался телефон чужого
    человека. Просьба владельца этим сужением не страдает: она про «клиент дал
    второй номер в диалоге», а это как раз принятый кандидат своей карточки.

    ⚠ И ТОЛЬКО ПРИНЯТЫЙ, А НЕ ЛЮБАЯ СТРОКА. `pending` — это вопрос, на который
    оператор ещё не ответил, `rejected` — ответ «это не его номер». Сделать
    основным то, что человек либо не рассматривал, либо отверг, значит обойти
    его решение молча.
    """
    phone = normalize_phone(raw)
    if phone is None:
        raise ApiError(
            "invalid_phone",
            "Не похоже на телефон. Введите российский номер целиком: +7 912 555-01-77.",
            status=422,
        )
    if client.phone == phone:
        # Уже основной: повторное нажатие не обязано ни падать, ни плодить
        # строку журнала. Кнопка могла приехать из устаревшего кадра карточки.
        return {"phone": phone, "changed": False}

    свои = await phone_candidates(db, client.id, include_merged=False)
    принятые = [r for r in свои if r.phone == phone and r.status == CANDIDATE_ACCEPTED]
    if not принятые:
        raise ApiError(
            "unknown_phone",
            "Этот номер за клиентом не числится. Впишите его в поле телефона.",
            status=422,
        )

    previous = client.phone
    if previous is not None:
        await _keep_as_extra(db, client, previous, actor=actor)

    client.phone = phone
    # ⚠ `phone_set_by_id` И `phone_set_at` СБРАСЫВАЕМ, А НЕ СТАВИМ И НЕ
    # ОСТАВЛЯЕМ. Обе колонки описывают ТЕКУЩИЙ номер карточки — «его набрал
    # человек с клавиатуры», — и по ним выбирается подпись. Оба соседних
    # решения оказались ложью, и обе проверены прогоном:
    #
    #   * ПОСТАВИТЬ их значило бы объявить рукописным номер, который обмен не
    #     набирал, а выбрал из уже доказанного. Тот же номер получал бы разное
    #     происхождение в зависимости от нажатой кнопки — ровно то, от чего
    #     письменно отказался соседний `resolve_phone_candidate`;
    #   * ОСТАВИТЬ прежнее значение — ложь наоборот: отметка досталась бы новому
    #     номеру от старого, и распознанный в переписке номер подписался бы
    #     «(со слов)». Именно это и вскрыл сторож обратного обмена.
    #
    # Цена сброса названа вслух: номер, который когда-то набрали руками и
    # вернули обменом, теряет подпись «(со слов)» и получает «(источник
    # неизвестен)». Это честнее: мы знаем, что номер человека, а откуда он у
    # нас — уже нет. Кто и когда набирал, осталось в журнале аудита.
    client.phone_set_by_id = None
    client.phone_set_at = None
    # Происхождение переезжает вместе с номером: диалог и канал берём у строки,
    # которая этот номер ДОКАЗЫВАЕТ. Обмен не выдумывает нового
    # доказательства, он выбирает из имеющихся.
    # ⚠ ДОКАЗЫВАЕТ ТОЛЬКО РАСПОЗНАННАЯ СТРОКА, А НЕ ЛЮБАЯ ПРИНЯТАЯ. У номера,
    # который уже был основным и вернулся обменом, есть СВОЯ строка `swap`, и
    # она принята. Взяв её за доказательство, мы объявили бы «из диалога»
    # номер, набранный с клавиатуры, и подставили бы в `phone_account_id`
    # канал, которого Авито не присылало, — а на этой колонке стоит
    # межканальная сверка (`inbound._apply_phone_evidence`).
    доказана = next(
        (r for r in принятые if r.source != CANDIDATE_SOURCE_SWAP and r.conversation_id), None
    )
    client.phone_conversation_id = доказана.conversation_id if доказана else None
    client.phone_account_id = await _account_of(db, доказана.conversation_id) if доказана else None
    await write_audit(
        db,
        user_id=actor.id,
        # ⚠ ПЕРВОЕ ЗАПОЛНЕНИЕ И ПЕРЕСТАНОВКА — РАЗНЫЕ СОБЫТИЯ, как в `set_phone`
        # и `resolve_phone_candidate`. Метрика «собрано телефонов» (06 §1.4)
        # считает ТОЛЬКО `client.phone_captured`: без этой развилки первый номер
        # карточки, поставленный обменом, выпал бы из отчёта молча.
        action="client.phone_captured" if previous is None else "client.phone_edited",
        entity="client",
        entity_id=str(client.id),
        details={
            # Колонки отчёта `by_bot`/`by_regex`/`by_manual` читают
            # `details.source`. Строка без него попала бы в итог и выпала бы из
            # всех трёх разбивок сразу. `regex` — если номер доказан
            # распознанной строкой; иначе происхождение нам уже неизвестно, и
            # врать про него в отчёте нельзя.
            "source": "regex" if доказана else "other",
            "previous": previous,
            "phone": phone,
            "by": "swap",
            # Канал для отбора по аккаунту отчёт берёт `LEFT JOIN conversations`
            # по `details.conversation_id` — тот же довод, что в `set_phone`.
            "conversation_id": str(доказана.conversation_id) if доказана else None,
        },
    )
    return {"phone": phone, "changed": True, "previous": previous}


async def _account_of(db: AsyncSession, conversation_id: uuid.UUID | None) -> uuid.UUID | None:
    """Канал, в котором шёл диалог, — для `clients.phone_account_id`.

    Колонка отвечает на вопрос «с какого НАШЕГО канала пришёл номер», и на нём
    стоит межканальная сверка (`inbound._apply_phone_evidence`). У обмена ответ
    есть: номер доказан конкретной перепиской, и канал у неё известен.
    """
    if conversation_id is None:
        return None
    return (
        await db.execute(
            sa.select(Conversation.account_id).where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()


async def _keep_as_extra(db: AsyncSession, client: Client, phone: str, *, actor: User) -> None:
    """Удержать номер в карточке дополнительным — строкой принятого кандидата.

    ⚠ СТРОКА МОЖЕТ УЖЕ БЫТЬ, И ТОГДА ЕЁ НАДО ПОДНЯТЬ, А НЕ ВСТАВИТЬ ВТОРУЮ.
    Уникальность (client_id, phone) не даст вставить дубль, а прежнее решение
    вполне могло быть `rejected`: оператор когда-то сказал «это не его номер»,
    потом номер всё же оказался основным, а теперь уезжает во вторые. Настоящее
    состояние — «его номер», и запись обязана это отражать.

    ⚠ ДИАЛОГА У ЭТОЙ СТРОКИ НЕТ, И ЭТО ГЛАВНОЕ В НЕЙ (правка после разбора,
    миграция 0070). Первая редакция ставила сюда диалог, в котором НАЖАЛИ
    кнопку, — и это было неправдой дважды.

    По смыслу: строка утверждала бы «номер назван в этой переписке», хотя он
    там не назывался; сообщения, из которого его вычитали, не существует.

    По последствию, и оно дороже: `conversation_id` — внешний ключ с
    `ON DELETE CASCADE`, а `clients.phone` чистка канала щадит НАМЕРЕННО
    («Клиентов НЕ трогаем: один и тот же человек мог писать в несколько
    каналов»). То есть обмен переносил бы номер из долговечного места в
    недолговечное: удалили посторонний канал — и прежний основной исчез из
    карточки совсем. На бою `account.deleted` случался 13 раз.

    Теперь у строк обмена диалога нет вовсе, и держит это CHECK
    `conversation_unless_swap`, а не соглашение в коде.
    """
    now = datetime.now(UTC)
    # ⚠ ВСТАВКА «ИЛИ НИЧЕГО», А ПОТОМ ПОДЪЁМ — ТЕМ ЖЕ ПРИЁМОМ, ЧТО У
    # `record_phone_candidate` ВЫШЕ, и по той же причине. Проверка «нет ли уже
    # такой строки» с последующей вставкой проигрывает второму диспетчеру:
    # SELECT не видит его незакоммиченную строку, а UNIQUE(client_id, phone)
    # отвечает нарушением уже в момент commit'а — то есть 500 вместо понятного
    # результата у того, кто нажал вторым.
    insert = dialect.insert(db)
    await db.execute(
        insert(ClientPhoneCandidate)
        .values(
            id=uuid.uuid4(),
            client_id=client.id,
            conversation_id=None,
            phone=phone,
            raw=phone,
            source=CANDIDATE_SOURCE_SWAP,
            status=CANDIDATE_ACCEPTED,
            detected_at=now,
            resolved_at=now,
            resolved_by_id=actor.id,
        )
        .on_conflict_do_nothing(index_elements=["client_id", "phone"])
    )
    # Строка могла существовать до нас — и с любым прежним решением, вплоть до
    # `rejected` («это не его номер»). Настоящее состояние теперь другое: номер
    # был основным, значит он его. Поднимаем ОТДЕЛЬНЫМ UPDATE'ом, а не веткой:
    # так один и тот же код обслуживает и «строки не было», и «строка была».
    await db.execute(
        sa.update(ClientPhoneCandidate)
        .where(
            ClientPhoneCandidate.client_id == client.id,
            ClientPhoneCandidate.phone == phone,
        )
        .values(status=CANDIDATE_ACCEPTED, resolved_at=now, resolved_by_id=actor.id)
    )


async def _accept_matching_candidates(
    db: AsyncSession, client: Client, phone: str, *, actor: User
) -> None:
    """Оператор ввёл руками тот же номер, что предлагала система, — вопрос снят.

    Без этого карточка выглядела бы издевательски: номер уже стоит в поле, а
    под ним по-прежнему висит предложение «распознан телефон +7 915 …, принять
    или отклонить». Отдельного действия человек тут не совершал, поэтому и
    отдельной строки журнала нет: ручной ввод уже записан своим событием.
    """
    rows = await phone_candidates(db, client.id, only_pending=True)
    now = datetime.now(UTC)
    for row in rows:
        if row.phone != phone:
            continue
        row.status = CANDIDATE_ACCEPTED
        row.resolved_at = now
        row.resolved_by_id = actor.id
        # Ручной ввод мог прийти без диалога (карточку открыли из поиска), а вот
        # кандидат всегда знает свою переписку. Если происхождение ещё не known —
        # берём его отсюда: номер, названный в диалоге и подтверждённый руками, —
        # самое доказанное, что бывает.
        if client.phone_conversation_id is None:
            client.phone_conversation_id = row.conversation_id


#: Потолок длины имени. Имя показывается в списке чатов, в шапке диалога и уезжает в
#: заявку лид-центра — везде это одна строка, и полотно на сто знаков там сломает вёрстку
#: раньше, чем принесёт пользу. Сто знаков — это «Анна Смирнова, соседка по площадке»,
#: чего для узнавания более чем достаточно.
MAX_NAME_LEN = 100


async def set_name(
    db: AsyncSession,
    client: Client,
    raw: str,
    *,
    actor: User,
) -> dict[str, Any]:
    """Записать имя клиента, введённое руками. Транзакцией владеет вызывающий.

    ЗАЧЕМ ЭТО ВООБЩЕ. Имя приходит из профиля Авито, а профиль человек заводил один раз
    и мог назвать как угодно: «Ак», «Продам всё», пусто. Диспетчер в разговоре узнаёт
    настоящее имя — и до сих пор ему некуда было его деть (требование заказчика 13.08).

    ПУСТАЯ СТРОКА — ЭТО ОЧИСТКА, А НЕ ОШИБКА. «Ак» хуже, чем ничего: под пустым именем
    карточка показывается как «Клиент», и это честно. Запретить очистку значило бы
    заставить диспетчера оставить заведомо неверное имя.

    ⚠ ОТМЕТКА `name_set_at` СТАВИТСЯ И ПРИ ОЧИСТКЕ, И ЭТО ГЛАВНОЕ В ФУНКЦИИ. Все три
    места, где имя приезжает из Авито, решают «писать или нет» по пустоте поля. Без
    отметки очищенное человеком имя вернулось бы следующей же дозагрузкой, и диспетчер
    решил бы, что кнопка не работает.
    """
    name = " ".join((raw or "").split())  # схлопываем пробелы: имя — одна строка
    if len(name) > MAX_NAME_LEN:
        raise ApiError(
            "name_too_long",
            f"Имя длиннее {MAX_NAME_LEN} знаков не поместится в карточку и в заявку.",
            status=422,
        )
    previous = client.name
    if previous == (name or None):
        # Ничего не изменилось — журнал не должен обрастать строками от повторного
        # «Сохранить» с тем же текстом.
        return {"name": client.name, "changed": False}

    client.name = name or None
    client.name_set_by_id = actor.id
    client.name_set_at = datetime.now(UTC)
    await write_audit(
        db,
        user_id=actor.id,
        action="client.name_captured" if previous is None else "client.name_edited",
        entity="client",
        entity_id=str(client.id),
        details={"name": client.name, "previous": previous},
    )
    return {"name": client.name, "changed": True}


async def set_phone(
    db: AsyncSession,
    client: Client,
    raw: str,
    *,
    actor: User,
    conversation_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Записать номер, введённый руками. Транзакцией владеет вызывающий.

    ДВА РАЗНЫХ СОБЫТИЯ ЖУРНАЛА, А НЕ ОДНО. Первое заполнение пишется как
    `client.phone_captured` с `source='manual'` — метрика «собрано телефонов»
    (06 §1.4, `app/services/stats.py::phones_collected`) считает именно её и
    уже имеет колонку `by_manual`, которая до сегодня всегда была нулевой:
    ручного ввода в системе просто не существовало. Исправление уже известного
    номера пишется отдельным `client.phone_edited`, иначе одна опечатка,
    поправленная трижды, дала бы «собрано 4 телефона» из одного.

    ДВОЙНИК НЕ БЛОКИРУЕТ СОХРАНЕНИЕ. Один и тот же человек законно имеет по
    карточке на каждый наш аккаунт, и отказ «такой номер уже есть» заставил бы
    оператора выбирать между правдой и возможностью сохранить. Номер
    сохраняется, а двойники возвращаются вызывающему — карточка покажет их
    строкой с кнопкой «Объединить».
    """
    phone = normalize_phone(raw)
    if phone is None:
        raise ApiError(
            "invalid_phone",
            "Не похоже на телефон. Введите российский номер целиком: +7 912 555-01-77.",
            status=422,
        )
    previous = client.phone
    if previous == phone:
        # Ничего не изменилось — журнал не должен обрастать строками от
        # повторного «Сохранить» по тому же номеру. Висящее предложение с этим
        # же номером всё равно снимаем: оно спрашивает про то, что уже сделано.
        await _accept_matching_candidates(db, client, phone, actor=actor)
        return {"phone": phone, "changed": False, "twins": []}

    client.phone = phone
    client.phone_set_by_id = actor.id
    client.phone_set_at = datetime.now(UTC)
    # Откуда номер — теперь не только в журнале, но и в строке: заявка обязана
    # спрашивать это на каждом сборе, а разбирать JSON аудита по всей истории она
    # не может. NULL, если ручку позвали без диалога (карточка открыта из поиска).
    client.phone_conversation_id = conversation_id
    # `phone_account_id` НЕ ставим: колонка отвечает на вопрос «с какого нашего
    # канала пришёл номер», и у введённого руками ответа нет. Подставить сюда
    # аккаунт открытого диалога значило бы дать `_apply_phone_evidence`
    # основание однажды подтвердить межканальную склейку номером, которого
    # Авито не присылал (см. app/models/client.py::phone_account_id).
    client.phone_account_id = None
    await write_audit(
        db,
        user_id=actor.id,
        action="client.phone_captured" if previous is None else "client.phone_edited",
        entity="client",
        entity_id=str(client.id),
        details={
            "source": "manual",
            "phone": phone,
            "previous": previous,
            # Диалог, из которого нажали, — по нему метрика телефонов считает
            # аккаунт (`LEFT JOIN conversations` в `_PHONES_SQL`). Без него
            # ручные телефоны выпадали бы из разбивки по каналам.
            "conversation_id": str(conversation_id) if conversation_id else None,
        },
    )
    await _accept_matching_candidates(db, client, phone, actor=actor)
    twins = await phone_twins(db, phone, exclude_id=client.id)
    return {
        "phone": phone,
        "changed": True,
        "twins": [short_view(c) for c in twins],
    }


# --- объединение --------------------------------------------------------------


def short_view(client: Client) -> dict[str, Any]:
    """Карточка одной строкой: двойник, подсказка, участник объединения.

    Имя публичное, потому что этим же составом полей отвечают ручки: двойник
    после ручного ввода номера и двойник после решения по распознанному —
    ОДНА строка в интерфейсе с одной кнопкой «Объединить». Собери её вторым
    словарём в маршруте, и два списка однажды разъедутся на поле, которого в
    одном из них нет.
    """
    return {
        "id": str(client.id),
        "name": client.name,
        "external_id": client.external_id,
        "phone": client.phone,
    }


_ADDRESS_FIELDS = (
    "address",
    "address_conversation_id",
    "address_set_by_id",
    "address_set_at",
    "address_candidate_id",
    "address_value",
)


async def _return_found_in_dialogs(
    db: AsyncSession,
    winner: Client,
    loser: Client,
    dialogs: list[uuid.UUID],
    snapshot: dict[str, Any],
) -> None:
    """Номер и адрес, пойманные в диалогах проигравшей уже после объединения,
    принадлежат ей и уходят вместе с диалогами.

    Иначе после «Разъединить» у победителя оставался чужой номер с подписью
    «(из диалога)» и ссылкой на диалог проигравшей, а у неё номера не было.
    Кандидаты, которые у проигравшей уже есть тем же значением, остаются на
    месте: уникальность (`client_id`, значение) не даёт их перенести.
    """
    if not dialogs:
        return
    phones = sa.orm.aliased(ClientPhoneCandidate)
    await db.execute(
        sa.update(ClientPhoneCandidate)
        .where(
            ClientPhoneCandidate.client_id == winner.id,
            ClientPhoneCandidate.conversation_id.in_(dialogs),
            ClientPhoneCandidate.phone.not_in(
                sa.select(phones.phone).where(phones.client_id == loser.id)
            ),
        )
        .values(client_id=loser.id)
        .execution_options(synchronize_session=False)
    )
    addresses = sa.orm.aliased(ClientAddressCandidate)
    await db.execute(
        sa.update(ClientAddressCandidate)
        .where(
            ClientAddressCandidate.client_id == winner.id,
            ClientAddressCandidate.conversation_id.in_(dialogs),
            ClientAddressCandidate.value.not_in(
                sa.select(addresses.value).where(addresses.client_id == loser.id)
            ),
        )
        .values(client_id=loser.id)
        .execution_options(synchronize_session=False)
    )
    if winner.phone_conversation_id in dialogs:
        if not loser.phone:
            loser.phone = winner.phone
            loser.phone_account_id = winner.phone_account_id
            loser.phone_conversation_id = winner.phone_conversation_id
            loser.phone_set_by_id = winner.phone_set_by_id
            loser.phone_set_at = winner.phone_set_at
        winner.phone = snapshot.get("phone")
        winner.phone_account_id = _as_uuid(snapshot.get("phone_account_id"))
        winner.phone_conversation_id = _as_uuid(snapshot.get("phone_conversation_id"))
        winner.phone_set_by_id = _as_uuid(snapshot.get("phone_set_by_id"))
        winner.phone_set_at = _as_dt(snapshot.get("phone_set_at"))
    if winner.address_conversation_id in dialogs:
        if not loser.address:
            for field in _ADDRESS_FIELDS:
                setattr(loser, field, getattr(winner, field))
        # Объединения до 24.09 адрес в снимок не писали — тогда просто снимаем.
        winner.address = snapshot.get("address")
        winner.address_value = snapshot.get("address_value")
        winner.address_conversation_id = _as_uuid(snapshot.get("address_conversation_id"))
        winner.address_candidate_id = _as_uuid(snapshot.get("address_candidate_id"))
        winner.address_set_by_id = _as_uuid(snapshot.get("address_set_by_id"))
        winner.address_set_at = _as_dt(snapshot.get("address_set_at"))


def _snapshot(client: Client) -> dict[str, Any]:
    """Всё, что объединение способно изменить у карточки, — до изменения.

    Список полей закрытый и совпадает с тем, что трогает :func:`merge_clients`;
    за этим следит `tests/unit/test_client_merge.py::test_unmerge_restores_exact_state`,
    который сравнивает ВСЕ колонки строки до и после «объединить + разъединить».
    Забытое поле — это карточка, которая после отката отличается от исходной,
    и никто этого не заметит, пока не начнут звонить.
    """
    return {
        "name": client.name,
        "phone": client.phone,
        "phone_account_id": str(client.phone_account_id) if client.phone_account_id else None,
        "phone_set_by_id": str(client.phone_set_by_id) if client.phone_set_by_id else None,
        "phone_set_at": client.phone_set_at.isoformat() if client.phone_set_at else None,
        # Пометка «имя ввёл человек» — часть состояния имени, и без неё откат вернул бы
        # имя, но потерял бы то, что оно ручное: следующее объединение снова затёрло бы его.
        "name_set_by_id": str(client.name_set_by_id) if client.name_set_by_id else None,
        "name_set_at": client.name_set_at.isoformat() if client.name_set_at else None,
        # Откуда номер и адрес — чтобы «Разъединить» вернуло их точно, если
        # автоматика заменит их находкой из диалогов проигравшей.
        "phone_conversation_id": _str_or_none(client.phone_conversation_id),
        "address": client.address,
        "address_value": client.address_value,
        "address_conversation_id": _str_or_none(client.address_conversation_id),
        "address_candidate_id": _str_or_none(client.address_candidate_id),
        "address_set_by_id": _str_or_none(client.address_set_by_id),
        "address_set_at": client.address_set_at.isoformat() if client.address_set_at else None,
    }


def _str_or_none(value: uuid.UUID | None) -> str | None:
    return str(value) if value else None


def _pick_name(winner: Client, loser: Client) -> str | None:
    """Имя карточки-победителя после объединения.

    ПРАВИЛО: имя берётся из карточки с ПОДТВЕРЖДЁННЫМ телефоном. Подтверждённый
    здесь — это «номер у карточки есть»: он либо вычитан из переписки самим
    клиентом, либо введён оператором со слов клиента, и в обоих случаях за ним
    стоит разговор, а за именем из Авито — только профиль, который человек
    заводил один раз и мог назвать как угодно («Продам всё», «Ак»).

    Телефон есть у обеих или ни у одной — имя победителя не трогаем; пустое
    имя победителя заполняется именем проигравшего, потому что «Клиент» вместо
    имени хуже любого настоящего имени.

    ⚠ 13.08. ИМЯ, ВВЕДЁННОЕ ЧЕЛОВЕКОМ, ПРАВИЛУ ВЫШЕ НЕ ПОДЧИНЯЕТСЯ. Правило целиком
    построено на том, что оба имени пришли из профиля Авито, и выбирает между двумя
    одинаково ненадёжными источниками. Имя, набранное диспетчером со слов клиента, к
    этой паре не относится: за ним стоит разговор, а не профиль.

    Живой случай, ради которого это написано: диспетчер вписал имя в карточку без
    телефона, потом объединил её с карточкой того же человека с другого аккаунта, где
    телефон есть, — и получил обратно авитошное «Ак». Правка молча пропала, и заметить
    это можно было только по следующему разговору.
    """
    # Введённое руками сильнее любого авитошного — с какой бы стороны оно ни было.
    if winner.name and winner.name_set_at:
        return winner.name
    if loser.name and loser.name_set_at:
        return loser.name
    winner_has, loser_has = bool(winner.phone), bool(loser.phone)
    if loser_has and not winner_has and loser.name:
        return loser.name
    return winner.name or loser.name


async def lock_cards(db: AsyncSession, *cards: Client) -> None:
    """`SELECT … FOR UPDATE` карточек по возрастанию id и перечитывание.

    Порядок по id — против взаимной блокировки двух встречных операций над
    одной парой. На SQLite (модульные тесты) `FOR UPDATE` молча ничем не
    является — гонка проверяется интеграционным тестом на Postgres.

    ⚠ ПЕРЕД КАРТОЧКАМИ — ИХ ДИАЛОГИ, В ТОМ ЖЕ ПОРЯДКЕ, ЧТО У ВХОДЯЩЕГО.
    Входящее берёт замок на диалог (`_upsert_conversation`, FOR UPDATE) и
    потом пишет в карточку (телефон). Объединение брало карточки и потом
    переезжало диалоги — встречный порядок, то есть взаимная блокировка:
    Postgres снимал бы одну из транзакций, и с равной вероятностью — приём
    сообщения клиента. Замок на диалоги первым выстраивает обе операции в один
    порядок «диалог → карточка».
    """
    ids = sorted({c.id for c in cards})
    await db.execute(
        sa.select(Conversation.id)
        .where(Conversation.client_id.in_(ids))
        .order_by(Conversation.id)
        .with_for_update()
    )
    await db.execute(
        sa.select(Client.id).where(Client.id.in_(ids)).order_by(Client.id).with_for_update()
    )
    for c in cards:
        await db.refresh(c)


async def merge_clients(
    db: AsyncSession,
    *,
    winner: Client,
    loser: Client,
    actor: User | None,
    auto: bool = False,
    rule: str | None = None,
    proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Объединить `loser` в `winner`. Транзакцией владеет вызывающий.

    ``actor=None`` — объединила автоматика (`workers/client_merge.py`): строка
    журнала без сотрудника, с `details.auto=true` и именем правила. Обратимо
    тем же «Разъединить», что и ручное.

    ⚠ ОБЕ КАРТОЧКИ БЕРУТСЯ ПОД ЗАМОК (`FOR UPDATE`, по возрастанию id) ДО
    ЛЮБОЙ ПРОВЕРКИ (правка 12.09). Воркер и ручное «Объединить» той же пары в
    одну секунду — под READ COMMITTED без замка обе транзакции проходят
    сторожей по старым строкам и фиксируются: кольцо A↔B, по которому
    `_upsert_client` ходит кругами. Redis-замок этого не решает: ручной
    маршрут его не берёт.

    ЧТО МЕНЯЕТСЯ: диалоги проигравшей карточки переезжают на победителя, у
    проигравшей проставляются `merged_into_id`/`merged_at`, у победителя может
    обновиться имя (:func:`_pick_name`) и подтянуться телефон, если своего не
    было.

    ЧТО НЕ МЕНЯЕТСЯ: телефон и идентификатор проигравшей карточки остаются на
    ней — они и есть «второй телефон» и «второй идентификатор» того же
    человека, которые карточка показывает списком. Поля автоматической склейки
    (`link_confidence`, `cross_account_since`, `link_phone_conflict_at`) не
    трогаются: они описывают свидетельства Авито, а не решение человека.
    """
    if winner.id == loser.id:
        raise ApiError("same_client", "Это одна и та же карточка", status=422)
    await lock_cards(db, winner, loser)
    if loser.merged_into_id is not None:
        raise ApiError(
            "already_merged",
            "Эта карточка уже объединена с другой — сначала разъедините её",
            status=422,
        )
    if winner.merged_into_id is not None:
        # Цепочка A -> B -> C неразъединяема: снимок в журнале описывает ОДНУ
        # операцию, и откат средней порвал бы обе. Внешний ключ такого не ловит
        # (он не проверяет циклы и глубину), поэтому запрет живёт здесь.
        raise ApiError(
            "target_is_merged",
            "Карточка, в которую объединяем, сама объединена с другой",
            status=422,
        )
    # ⚠ ВТОРАЯ ПОЛОВИНА ТОГО ЖЕ ЗАПРЕТА, И ЕЁ НЕ БЫЛО (найдено 14.08).
    #
    # Проверки выше ловят «проигравшая уже объединена» и «победитель сам объединён».
    # Но цепочка строится и третьим способом: A объединили в B (у B поле пустое —
    # он победитель), потом B объединяют в C. Обе проверки проходят, и получается
    # A -> B -> C.
    #
    # ЦЕНА. `unmerge(A)` берёт победителя из `A.merged_into_id` — это B, — а диалоги
    # уже уехали к C. UPDATE по `client_id == B.id` не находит ни строки, и карточка
    # разъединяется ПУСТОЙ, с бодрым «Диалогов возвращено: 0». То есть «Разъединить»
    # молча не возвращает исходное — а на этом обещании держится всё доверие к
    # объединению, тем более к автоматическому.
    #
    # Запрет, а не переподвязка: снимок в журнале описывает ОДНУ операцию, и
    # переподвязать группу — значит сделать откат неоднозначным. Осознанный отказ
    # в редком случае лучше молчаливой потери в частом.
    has_own_group = (
        await db.execute(
            sa.select(sa.func.count()).select_from(Client).where(Client.merged_into_id == loser.id)
        )
    ).scalar_one()
    if has_own_group:
        raise ApiError(
            "loser_has_merged",
            "В эту карточку уже объединяли другие — сначала разъедините их",
            status=422,
        )

    moved = [
        str(row)
        for row in (
            await db.execute(sa.select(Conversation.id).where(Conversation.client_id == loser.id))
        )
        .scalars()
        .all()
    ]
    before = {"winner": _snapshot(winner), "loser": _snapshot(loser)}
    # Совпадение номеров фиксируется ДО переноса телефона: после него оба поля
    # равны всегда, и «телефоны совпали» стало бы самоисполняющимся.
    phone_match = bool(winner.phone) and winner.phone == loser.phone

    if moved:
        await db.execute(
            sa.update(Conversation)
            .where(Conversation.client_id == loser.id)
            .values(client_id=winner.id)
        )
    # ⚠ РАСПОЗНАННЫЕ НОМЕРА НИКУДА НЕ ПЕРЕЕЗЖАЮТ (решение владельца 19.08,
    # находка аудита L-013). Раньше кандидаты проигравшего переносились на
    # победителя, а совпавшие по номеру УДАЛЯЛИСЬ — и разъединение не могло
    # их вернуть: восстанавливать было неоткуда. Докстринг `unmerge_clients`
    # при этом обещал вернуть «ТОЧНО в исходное состояние», то есть код
    # противоречил собственному комментарию. На бою механизм рабочий: 16
    # объединений и 14 разъединений в журнале.
    #
    # Чинится корень, а не следствие: если данные не двигать, восстанавливать
    # нечего. Победитель видит и свои номера, и номера присоединённых карточек —
    # чтение ходит по связи `merged_into_id` (:func:`phone_candidates`), тем же
    # приёмом, каким карточка уже читает присоединённые (`_merged_into_me`).
    # Заодно снимается упор в UNIQUE(client_id, phone): одинаковые номера на
    # двух карточках больше не конфликтуют, потому что остаются каждый у себя.
    winner.name = _pick_name(winner, loser)
    if winner.phone is None and loser.phone is not None:
        # Телефон переезжает ТОЛЬКО в пустое место. Иначе объединение затирало
        # бы номер, по которому уже звонили, номером из карточки, о которой
        # известно меньше.
        winner.phone = loser.phone
        winner.phone_account_id = loser.phone_account_id
        winner.phone_set_by_id = loser.phone_set_by_id
        winner.phone_set_at = loser.phone_set_at
    loser.merged_into_id = winner.id
    loser.merged_at = datetime.now(UTC)
    # Человек переиграл свой же откат — память о разъединении снимается. У
    # автоматики такой строки быть не может: она перед склейкой проверяет её.
    await db.execute(
        sa.delete(ClientMergeVeto).where(
            ClientMergeVeto.a_id == min(winner.id, loser.id),
            ClientMergeVeto.b_id == max(winner.id, loser.id),
        )
    )

    await write_audit(
        db,
        user_id=actor.id if actor else None,
        action="client.merged",
        entity="client",
        entity_id=str(winner.id),
        details={
            "source_id": str(loser.id),
            "target_id": str(winner.id),
            "conversation_ids": moved,
            "phone_match": phone_match,
            # Автоматику видно по `auto`/`rule`/`proof` (доказательства: диалоги и
            # аккаунты, по которым обе карточки назвали номер); у ручного их нет.
            **({"auto": True, "rule": rule, "proof": proof or {}} if auto else {}),
            "before": before,
            # ⚠ СОСТОЯНИЕ ПОСЛЕ — ЧТОБЫ РАЗЪЕДИНЕНИЕ НЕ СТИРАЛО ЧУЖУЮ РАБОТУ
            # (28.08). Откат восстанавливал победителя из `before` БЕЗУСЛОВНО, а
            # между объединением и разъединением карточку правят свободно: ни
            # `set_name`, ни `set_phone`, ни ручка телефона не смотрят на
            # `merged_into_id`. Номер, вписанный ПОСЛЕ объединения, исчезал при
            # разъединении с обеих карточек — молча.
            #
            # Снимок «после» позволяет отличить «поле осталось таким, каким его
            # оставило объединение» от «поле с тех пор изменили». Для диалогов
            # эта же асимметрия обработана давно: возвращаются только те, что
            # перечислены в `conversation_ids`, а появившиеся позже остаются у
            # победителя.
            "after": _snapshot(winner),
        },
    )
    return {
        "target": short_view(winner),
        "source": short_view(loser),
        "moved_conversations": len(moved),
        "conversation_ids": moved,
        "phone_match": phone_match,
    }


async def last_merge_row(db: AsyncSession, loser_id: uuid.UUID) -> AuditLog | None:
    """Строка журнала об объединении, которым эту карточку увели.

    Ищется по `entity_id` ПОБЕДИТЕЛЯ (индекс `idx_audit_entity` —
    `(entity, entity_id, created_at)`, миграция 0004), а из найденных берётся
    та, где `details.source_id` — эта карточка. Отбор по JSON идёт уже по
    считанным строкам, а не в SQL: одинаковый предикат на JSONB в Postgres и на
    JSON в SQLite не пишется, а объединений у одной карточки единицы.
    """
    loser = await db.get(Client, loser_id)
    if loser is None or loser.merged_into_id is None:
        return None
    rows = (
        await db.execute(
            sa.select(AuditLog)
            .where(
                AuditLog.action == "client.merged",
                AuditLog.entity == "client",
                AuditLog.entity_id == str(loser.merged_into_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
    ).scalars()
    for row in rows:
        if (row.details or {}).get("source_id") == str(loser_id):
            return row
    return None


async def unmerge_clients(db: AsyncSession, *, loser: Client, actor: User) -> dict[str, Any]:
    """Вернуть карточку `loser` из объединения — ТОЧНО в исходное состояние.

    Восстанавливается по снимку из журнала, а не по догадке: имя и телефон
    победителя откатываются к записанным значениям, обратно переезжают ровно те
    диалоги, что переезжали сюда (список в `details.conversation_ids`), — то
    есть диалоги, появившиеся у победителя ПОСЛЕ объединения, остаются у него.
    Это и значит «точно исходное»: вернуть ему чужой новый диалог было бы не
    откатом, а второй ошибкой.

    ⚠ ОДНО ИСКЛЮЧЕНИЕ, И ОНО НЕ ПРОТИВОРЕЧИТ СКАЗАННОМУ (правка 12.09): диалог,
    заведённый ПОСЛЕ объединения на идентификатор Авито ПРОИГРАВШЕЙ карточки,
    — не «чужой новый», а её собственный: `_upsert_client` увёл его к
    победителю только потому, что карточка была объединена, и записал это в
    `conversations.origin_client_id`. Такие диалоги возвращаются тоже — иначе
    ошибочная склейка, замеченная через неделю, отменялась бы наполовину.

    РАЗЪЕДИНЕНИЕ ЗАПОМИНАЕТСЯ (`client_merge_vetoes`): автоматическое
    объединение по телефону эту пару больше не тронет — только подсказка.

    Снимка нет — отката нет. Угадать, что имя «Анна Смирнова» пришло от
    проигравшей карточки, а не стояло у победителя изначально, нельзя, и
    отгадка здесь стоит перепутанного имени в боевой карточке.
    """
    if loser.merged_into_id is None:
        raise ApiError("not_merged", "Эта карточка ни с чем не объединена", status=422)
    победитель_id = loser.merged_into_id
    победитель = await db.get(Client, победитель_id)
    if победитель is not None:
        await lock_cards(db, loser, победитель)
        if loser.merged_into_id != победитель_id:
            raise ApiError("not_merged", "Эта карточка ни с чем не объединена", status=422)
    row = await last_merge_row(db, loser.id)
    if row is None:
        raise ApiError(
            "merge_record_missing",
            "В журнале нет записи об этом объединении — разъединить автоматически нельзя",
            status=409,
        )
    details = row.details or {}
    winner = await db.get(Client, loser.merged_into_id)
    if winner is None:
        raise ApiError("not_found", "Карточка, в которую объединяли, не найдена", status=404)

    before = details.get("before") or {}
    snapshot = before.get("winner") or {}
    # ⚠ ВОЗВРАЩАЕМ ТОЛЬКО НЕТРОНУТОЕ (28.08).
    #
    # Раньше поля победителя восстанавливались из снимка БЕЗУСЛОВНО. Но между
    # объединением и разъединением карточку правят свободно — ни `set_name`, ни
    # `set_phone`, ни `PUT /clients/{id}/phone` не смотрят на `merged_into_id`.
    # Типичный случай: карточки объединили, после этого клиент продиктовал номер
    # и диспетчер вписал его победителю; через день выяснилось, что люди разные,
    # другой диспетчер нажал «Разъединить» — и номер, по которому собирались
    # звонить, исчез с ОБЕИХ карточек. Ни ошибки, ни следа: в журнале обычное
    # разъединение.
    #
    # `after` — снимок, оставленный самим объединением. Поле, которое с тех пор
    # не менялось, возвращаем; изменённое оставляем как есть: это чужая работа,
    # и откат объединения её не касается. Тот же принцип, что у диалогов —
    # «появившиеся у победителя ПОСЛЕ объединения остаются у него».
    #
    # Строки журнала БЕЗ `after` (объединения до этой правки) откатываем как
    # прежде, целиком: другого источника правды у них нет, а отказаться от
    # отката значило бы запереть их навсегда.
    после = details.get("after")
    сейчас = _snapshot(winner) if после else None

    def нетронуто(поле: str) -> bool:
        if после is None or сейчас is None:
            return True  # старая запись журнала — ведём себя как раньше
        return сейчас.get(поле) == после.get(поле)

    if нетронуто("name"):
        winner.name = snapshot.get("name")
        winner.name_set_by_id = _as_uuid(snapshot.get("name_set_by_id"))
        winner.name_set_at = _as_dt(snapshot.get("name_set_at"))
    if нетронуто("phone"):
        winner.phone = snapshot.get("phone")
        winner.phone_account_id = _as_uuid(snapshot.get("phone_account_id"))
        winner.phone_set_by_id = _as_uuid(snapshot.get("phone_set_by_id"))
        winner.phone_set_at = _as_dt(snapshot.get("phone_set_at"))

    conversation_ids = [_as_uuid(v) for v in details.get("conversation_ids") or []]
    back = [cid for cid in conversation_ids if cid is not None]
    if back:
        await db.execute(
            sa.update(Conversation)
            .where(Conversation.id.in_(back), Conversation.client_id == winner.id)
            .values(client_id=loser.id)
        )
    # Диалоги, заведённые на идентификатор проигравшей ПОСЛЕ объединения, —
    # см. докстринг. `origin_client_id` при этом обнуляется: диалог снова под
    # своей карточкой, и повторное объединение поставит его заново.
    появились_позже = list(
        (
            await db.execute(
                sa.select(Conversation.id).where(
                    Conversation.client_id == winner.id,
                    Conversation.origin_client_id == loser.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if появились_позже:
        await db.execute(
            sa.update(Conversation)
            .where(Conversation.id.in_(появились_позже))
            .values(client_id=loser.id, origin_client_id=None)
        )
    await _return_found_in_dialogs(db, winner, loser, back + появились_позже, snapshot)
    loser.merged_into_id = None
    loser.merged_at = None
    insert = dialect.insert(db)
    await db.execute(
        insert(ClientMergeVeto)
        .values(
            a_id=min(winner.id, loser.id),
            b_id=max(winner.id, loser.id),
            created_by_id=actor.id,
            created_at=datetime.now(UTC),
            merge_audit_id=row.id,
        )
        .on_conflict_do_nothing(index_elements=["a_id", "b_id"])
    )

    await write_audit(
        db,
        user_id=actor.id,
        action="client.unmerged",
        entity="client",
        entity_id=str(winner.id),
        details={
            "source_id": str(loser.id),
            "target_id": str(winner.id),
            "merge_audit_id": row.id,
            "conversation_ids": [str(cid) for cid in back],
            "returned_after_merge": len(появились_позже),
            "was_auto": bool(details.get("auto")),
            "veto": True,
        },
    )
    return {
        "target": short_view(winner),
        "source": short_view(loser),
        "moved_conversations": len(back) + len(появились_позже),
        "conversation_ids": [str(cid) for cid in (*back, *появились_позже)],
    }


def _as_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _as_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # SQLite отдаёт наивное время, Postgres — с зоной. Возвращаем как есть в
    # той же форме, в какой хранили: приведение к UTC здесь сдвинуло бы метку
    # ровно на смещение сервера в одном из двух диалектов.
    return parsed


# --- чтение карточки ----------------------------------------------------------


async def _merged_into_children(db: AsyncSession, client_ids: list[uuid.UUID]) -> list[Client]:
    """Карточки, присоединённые к любой из переданных (один уровень вглубь)."""
    if not client_ids:
        return []
    return list(
        (
            await db.execute(
                sa.select(Client)
                .where(Client.merged_into_id.in_(client_ids))
                .order_by(Client.merged_at)
            )
        )
        .scalars()
        .all()
    )


async def _merged_into_me(db: AsyncSession, client_id: uuid.UUID) -> list[Client]:
    return list(
        (
            await db.execute(
                sa.select(Client)
                .where(Client.merged_into_id == client_id)
                .order_by(Client.merged_at)
            )
        )
        .scalars()
        .all()
    )


def _phone_source(client: Client, accepted: list[ClientPhoneCandidate]) -> str:
    """Откуда взялся номер, стоящий в карточке. Одно слово для подписи.

    Порядок проверок — от самого достоверного к самому слабому. Ручной ввод
    первый: у него есть автор и время, и он побеждает совпадение с кандидатом
    (человек мог набрать тот же номер сам, и подписать это «из диалога» значило
    бы приписать его работу автоматике).
    """
    if client.phone is None:
        return "none"
    if client.phone_set_at is not None:
        return "manual"
    # ⚠ СТРОКА ОБМЕНА ДОКАЗАТЕЛЬСТВОМ ИЗ ДИАЛОГА НЕ ЯВЛЯЕТСЯ (правка 09.09).
    # `source='swap'` означает «номер был основным до обмена», а не «номер
    # назван в переписке»: ни сообщения, ни диалога у неё нет. Без этого
    # фильтра выходила бы ложь с обратным ходом: набрал номер руками, поменял
    # местами туда и обратно — и карточка подписывает его «из диалога», потому
    # что нашла СВОЮ ЖЕ строку обмена. Поправка «фильтровать по accepted» это
    # не лечит: строка обмена именно accepted.
    # Голос — ПЕРЕД «из диалога» и после ручного ввода: основной, заполненный
    # автозаписью из расшифровки, иначе выглядел бы написанным руками клиента,
    # а доказательство у него машинное — оператор обязан знать, что сверять
    # надо со звуком (ослышка Whisper даёт правдоподобный номер, у опечатки
    # чаще ломается длина). Ручной ввод выше по-прежнему побеждает.
    if any(row.phone == client.phone and row.source == CANDIDATE_SOURCE_VOICE for row in accepted):
        return "voice"
    if any(row.phone == client.phone and row.source != CANDIDATE_SOURCE_SWAP for row in accepted):
        return "dialog"
    # Бот со своим шагом «Вопрос», ответ ассистенту Авито, строки старше этой
    # правки, а с 09.09 ещё и номер, вернувшийся обменом: мы знаем, что он
    # человека, а откуда он у нас — уже нет. Врать «из диалога» нельзя.
    return "other"


def address_candidate_view(row: ClientAddressCandidate) -> dict[str, Any]:
    """Один распознанный адрес для экрана. Состав полей — контракт с интерфейсом.

    `raw` здесь не для полноты: предложение, которое нельзя сверить с тем, что
    написал клиент, оператор либо примет не глядя, либо перестанет замечать. У
    адреса это вдвойне так — по нему поедет мастер.
    """
    части = {имя: getattr(row, имя) for имя in _ЧАСТИ_АДРЕСА if getattr(row, имя) is not None}
    return {
        "id": str(row.id),
        # Состояние в ответе — не для полноты: экран после решения показывает
        # его исход, и добывать этот исход вторым запросом значило бы завести
        # второй путь к тому же ответу. У телефона поле есть с 12 августа.
        "status": row.status,
        "value": row.value,
        "street": row.street,
        "house": row.house,
        "parts": части,
        "raw": row.raw,
        "level": row.level,
        "settlement": row.settlement,
        "settlement_type": row.settlement_type,
        "locality": row.locality,
        # «place» — место без улицы: район, пункт, массив (13.09).
        "kind": row.kind,
        "district": row.district,
        "area": row.area,
        "geo": geo_view(row),
        "conversation_id": str(row.conversation_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "message_at": row.message_at.isoformat() if row.message_at else None,
        "detected_at": row.detected_at.isoformat(),
    }


def geo_view(row: ClientAddressCandidate) -> dict[str, Any] | None:
    """Вердикт карты по строке — для экрана. NULL-статус — строка старше правки."""
    if row.geo_status is None:
        return None
    return {
        "status": row.geo_status,
        "formatted": row.geo_formatted,
        "lat": row.geo_lat,
        "lon": row.geo_lon,
        "provider": row.geo_provider,
        "checked_at": row.geo_checked_at.isoformat() if row.geo_checked_at else None,
        # Степень точки одним словом (18.09): экран читает ТОЛЬКО его, хвост
        # провайдера («~approx») и `kind` на экране для этого не разбираются —
        # иначе два пути к одному признаку. Место — всегда «approx».
        "precision": candidate_precision(row),
        # Что карта нашла, когда не выбрала один дом (12.09): показывается
        # оператору как информация к «изменить»; строка без вариантов отдаёт
        # пустой список.
        "variants": list(row.geo_variants or []),
        # Причина степени словами (пакет 6.0а, I-9): имя правила из `trace` и
        # его подпись — под записанным автоадресом и у предложения; None —
        # вердикт самой карты, правила не было. `suggest` — решение правила
        # ждёт нажатия оператора (политика `suggest`): в карточку само не идёт,
        # точка на экране настоящая (`precision` считается без хвоста).
        "rule": candidate_rule(row),
        "rule_label": geocode.rule_label(candidate_rule(row)),
        "suggest": geocode.point_is_suggest(row.geo_provider),
    }


def _operator_writes(
    row: ClientAddressCandidate, *, autofill: bool, policy: Mapping[str, str]
) -> bool:
    """Строку со степенью автоматика сама не запишет — её пишет оператор.

    ПРОВЕРКА 24.09. Выключенная автозапись обещала оператору «подтвердить или
    не адрес», а у строки была только «Не адрес»: принять найденный картой
    адрес можно было лишь перепечаткой через «изменить». Второй случай —
    решение правила, которое лестница понизила до `suggest` после суда:
    автозапись его не берёт (`geocode.rule_writes_card`), как и свежее
    предложение с хвостом `~suggest`, — и принимается оно так же, одним
    нажатием. Свежее предложение степени не имеет, его кнопку экран узнаёт по
    `geo.suggest`.
    """
    if candidate_grade(row) is None:
        return False
    if not autofill:
        return True
    rule = candidate_rule(row)
    return rule is not None and geocode.rule_policy(policy, rule) == geocode.POLICY_SUGGEST


def candidate_rule(row: ClientAddressCandidate) -> str | None:
    """Имя правила, которым решена строка, — из `trace.rule`; следа нет или
    решала сама карта — None. Читают экран и автозапись (политика правила)."""
    след = row.trace if isinstance(row.trace, dict) else None
    правило = след.get("rule") if след else None
    return str(правило) if правило else None


def _geo_на_экран(row: ClientAddressCandidate, карта_включена: bool) -> dict[str, Any] | None:
    """Вердикт для экрана: при выключенной карте `pending` — это «не спрашивали»."""
    if not карта_включена and row.geo_status in (None, geocode.GEO_PENDING):
        return None
    return geo_view(row)


def _карта_нашла_дом(row: ClientAddressCandidate) -> bool:
    """Точный дом — да; варианты — да, но у невода C только с улицей слово в
    слово в одном из вариантов («Липовая 6» → три Липовых по области —
    адрес; «Просто 3» → «ГСК Простор» — нет, стенд 14.09)."""
    if row.geo_status == geocode.GEO_EXACT:
        return True
    if not row.geo_variants:
        return False
    if row.level != address_parse.LEVEL_C:
        return True
    # Последнее СЛОВО основы, не число («Ленина 2-я» → «ленина»), и с тем же
    # допуском на падеж и опечатку, что у вердикта («Октябрьской 5» ↔ «ул
    # Октябрьская, 5», ревью 14.09).
    слова = [ч for ч in address_parse.street_core(row.street or "") if not ч.isdigit()]
    if not слова:
        return False
    ядро = слова[-1].lower()
    for v in row.geo_variants:
        if not isinstance(v, dict):
            continue
        улица_варианта = str(v.get("formatted") or "").split(",")[0].lower()
        # Основа слово в слово (без допуска на опечатку: «Просто» ↔ «Простор»
        # — не улица), падеж не мешает.
        if any(
            geocode._основа(ядро) == geocode._основа(w)
            for w in re.findall(r"[а-яё]{3,}", улица_варианта)
        ):
            return True
    return False


def _address_source(client: Client, адреса: list[ClientAddressCandidate]) -> str:
    """Откуда взялся адрес в карточке: `none`, `manual`, `auto`, `dialog`, `other`.

    Те же случаи, что у телефона, плюс `auto` — записала карта без человека.
    ⚠ С 11.09 ПРОИСХОЖДЕНИЕ ЧИТАЕТСЯ ПО СВЯЗИ `address_candidate_id`, А НЕ
    СРАВНЕНИЕМ ТЕКСТА: в поле лежит строка карты, в кандидате — «улица, дом»
    клиента, и текстом они не равны никогда. Строки до 11.09 связи не имеют —
    для них остаётся старое сравнение, чтобы подпись не пропала задним числом.
    """
    if client.address is None:
        return "none"
    if client.address_set_at is not None:
        return "manual"
    источник = _строка_источник(client, адреса)
    if источник is not None:
        return "auto" if источник.resolved_by_id is None else "dialog"
    if any(row.value == client.address and row.status == CANDIDATE_ACCEPTED for row in адреса):
        return "dialog"
    return "other"


def _без_дублей(
    строки: list[ClientAddressCandidate], источник: ClientAddressCandidate | None
) -> list[tuple[ClientAddressCandidate, dict[str, str]]]:
    """Один адрес — одно предложение (бой 13.09: по две строки на карточку).

    Строки одного места (`same_address`, либо карта свела их в один дом —
    одинаковый `geo_formatted`) показываются одной: остаётся та, где карта
    подтвердила дом, затем — где больше частей, затем более ранняя; части
    скрытых строк подставляются в показ (строки в базе не трогаем — это
    чтение). Строка того же места, что уже записанный адрес карточки, не
    показывается вовсе — предлагать нечего.
    """
    # УЛИЦА БЕЗ ДОМА, ЗА КОТОРОЙ ПРИШЁЛ ДОМ (владелец 14.09, Тула: «Зареченский
    # район. Ул. Садовая» → «Ул. Садовая, д. 47» — «две улицы в одной
    # карточке»). Место-улица — шаг к дому, а не второй адрес: когда у той же
    # улицы есть строка с домом (или она уже записана), место не показывается.
    все = [*строки, *([источник] if источник is not None else [])]
    дома = {
        address_parse.street_core(r.street)
        for r in все
        if r.kind == address_parse.KIND_HOUSE and r.street and r.house
    }
    # «ул ялесная» → «Ул Лесная» → «Лесная ул 9» (владелец 14.09, Балашиха):
    # клиент поправил себя, карта подтвердила дом — улица с опечаткой, которую
    # карта не нашла, больше не предложение: дом того же диалога её заменил.
    подтверждённые_дома = [
        r for r in все if r.kind == address_parse.KIND_HOUSE and r.geo_status == geocode.GEO_EXACT
    ]
    группы: list[list[ClientAddressCandidate]] = []
    for row in строки:
        if источник is not None and (
            _то_же_место(row, источник)
            or _тот_же_дом_без_корпуса(row, источник)
            or _поглощена_источником(row, источник)
        ):
            continue
        if (
            row.kind == address_parse.KIND_PLACE
            and row.street
            and not row.settlement
            and address_parse.street_core(row.street) in дома
        ):
            continue
        if (
            row.kind == address_parse.KIND_PLACE
            and row.street
            and not row.settlement
            and row.geo_status != geocode.GEO_EXACT
            and any(
                d.conversation_id == row.conversation_id and d.detected_at > row.detected_at
                for d in подтверждённые_дома
            )
        ):
            continue
        for группа in группы:
            # Со ВСЕМИ строками группы, не с любой: семья не транзитивна —
            # голый «31» родня и «31 к 1», и «31 к 2», а те друг другу нет
            # (ревью 15.09: поправка «нет, 31 к 2» пропадала с экрана).
            if all(одно_место(row, r) for r in группа):
                группа.append(row)
                break
        else:
            группы.append([row])

    def вес(r: ClientAddressCandidate) -> tuple[int, int, int, float]:
        частей = sum(1 for ч in _ЧАСТИ_АДРЕСА if getattr(r, ч))
        return (
            1 if r.geo_status == geocode.GEO_EXACT else 0,
            # «31 А» точнее «31» той же семьи (стенд 15.09).
            len(geocode.house_key(r.house)) if r.kind == address_parse.KIND_HOUSE else 0,
            частей,
            -r.detected_at.timestamp(),
        )

    итог: list[tuple[ClientAddressCandidate, dict[str, str]]] = []
    for группа in группы:
        лучшая = max(группа, key=вес)
        части: dict[str, str] = {}
        # Позднее значение части побеждает раннее — как при записи.
        for r in sorted(группа, key=lambda r: r.detected_at):
            for ч in _ЧАСТИ_АДРЕСА:
                if getattr(r, ч) is not None:
                    части[ч] = getattr(r, ч)
        итог.append((лучшая, части))
    итог.sort(key=lambda пара: пара[0].detected_at)
    return итог


def одно_место(a: ClientAddressCandidate, b: ClientAddressCandidate) -> bool:
    """Одно место — или дом одной семьи: «31» и «31 А», «3» и «3/1», «2» и
    «2 стр 2» на одной улице (стенд 15.09: 185 карточек с «двумя домами», почти
    все такие). Один человек в одном разговоре не называет два соседних дома —
    он уточняет свой; голый номер и номер с литерой или корпусом — одно место.
    Две разные литеры («31 А» и «31 Б») — два дома, семьёй не считаются."""
    return _то_же_место(a, b) or _одна_семья_дома(a, b)


def _одна_семья_дома(a: ClientAddressCandidate, b: ClientAddressCandidate) -> bool:
    if a.kind != address_parse.KIND_HOUSE or b.kind != address_parse.KIND_HOUSE:
        return False
    ядро = address_parse.street_core(a.street)
    if not ядро or ядро != address_parse.street_core(b.street):
        return False
    for поле in ("settlement", "locality"):
        # Пункт или город назван у обеих и разный — не семья (ревью 15.09).
        x, y = (getattr(a, поле) or "").lower(), (getattr(b, поле) or "").lower()
        if x and y and x != y:
            return False
    if (
        a.geo_status == geocode.GEO_EXACT
        and b.geo_status == geocode.GEO_EXACT
        and a.geo_formatted
        and b.geo_formatted
        and not _карта_об_одном(a.geo_formatted, b.geo_formatted)
    ):
        # Карта главнее слов: «Ленина 5» на улице Ленина и «Ленина 5 А» на
        # проспекте Ленина (или в другом городе) — не семья (ревью 15.09).
        return False
    тип_a, тип_b = address_parse.street_type(a.street), address_parse.street_type(b.street)
    if тип_a is not None and тип_b is not None and тип_a != тип_b:
        return False
    номер_a = re.match(r"\d+", (a.house or "").strip())
    номер_b = re.match(r"\d+", (b.house or "").strip())
    if not номер_a or not номер_b or номер_a.group(0) != номер_b.group(0):
        return False
    ключ_a, ключ_b = geocode.house_key(a.house), geocode.house_key(b.house)
    return ключ_a != ключ_b and номер_a.group(0) in (ключ_a, ключ_b)


def _карта_об_одном(a: str, b: str) -> bool:
    """Две строки карты — об одной улице и одном городе (дом не сверяется).
    Тип улицы, названный обеими, обязан совпасть; названный одной — не спор."""

    def куски(formatted: str) -> set[tuple[frozenset[str], str | None]]:
        out: set[tuple[frozenset[str], str | None]] = set()
        for к in geocode.formatted_key(formatted):
            if к[:1].isdigit():
                continue
            слова = к.split()
            типы = [w for w in слова if w in _ТИПЫ_УЛИЦ_КАРТЫ]
            out.add((frozenset(w for w in слова if w not in типы), типы[0] if типы else None))
        return out

    типы_а, типы_б = dict(куски(a)), dict(куски(b))
    if типы_а.keys() != типы_б.keys():
        return False
    return all(
        типы_а[ядро] is None or типы_б[ядро] is None or типы_а[ядро] == типы_б[ядро]
        for ядро in типы_а
    )


#: Полные слова типов улиц, как их пишет `formatted_key`.
_ТИПЫ_УЛИЦ_КАРТЫ = frozenset(geocode._ТИПЫ_УЛИЦ.values())


def _тот_же_дом_без_корпуса(row: ClientAddressCandidate, источник: ClientAddressCandidate) -> bool:
    """«Чкалова, 4» при записанном «Чкалова, 4 к 3» (бой 13.09): тот
    же номер дома без корпуса — записанное точнее, предлагать нечего. В другую
    сторону не работает: «4 к 3» при записанном «4» — уточнение, его показываем."""
    номер = re.match(r"\d+", (row.house or "").strip())
    return (
        номер is not None
        and geocode.house_key(row.house) == номер.group(0)
        and _одна_семья_дома(row, источник)
    )


_ЧИСЛА = re.compile(r"\d+")
_ЧТО_НЕ_ЧИСЛО = re.compile(r"[^0-9a-zа-яё]+")


def _числа_источника(источник: ClientAddressCandidate) -> set[str]:
    """Все числа записанного адреса: дом, части, числа улицы и массива
    («мкр 11», «45-й комплекс»)."""
    куски = [источник.house, источник.street, источник.settlement or "", источник.area or ""]
    куски += [getattr(источник, ч) or "" for ч in _ЧАСТИ_АДРЕСА]
    return {n for к in куски for n in _ЧИСЛА.findall(к or "")}


def _слова_источника(источник: ClientAddressCandidate) -> set[str]:
    куски = [
        источник.value,
        источник.geo_formatted or "",
        источник.settlement or "",
        источник.area or "",
    ]
    return {w for к in куски for w in _ЧТО_НЕ_ЧИСЛО.split((к or "").lower()) if w}


def _поглощена_источником(row: ClientAddressCandidate, источник: ClientAddressCandidate) -> bool:
    """Строка — обрубок того же адреса, что уже записан (владелец 19.09,
    Нефтеюганск: «14 мкр; 27» → «кв 9» дали место «мкр 14» и «дом» «27 мкр,
    9», а карточка — «мкр 14, 27, кв 9»). «Сначала думать, потом давать
    адрес»: обрубки не показываются — предлагать нечего.

    Место поглощено, когда все его слова и числа есть в записанном адресе
    («микрорайон 9» при доме с area «микрорайон 9»). «Дом» поглощён, когда
    он из той же серии реплик (сутки) и ВСЕ его числа — числа записанного
    адреса, а слова улицы — слова записанного адреса: «27 мкр, 9» при «мкр
    14, 27, кв 9». Настоящий второй адрес («Гагрина, 12» при «85 квартал,
    17») не поглощается: у него свои слова и свои числа — он остаётся
    показанным как «также назван».

    ⚠ ДРУГОЙ НОМЕР НА ТОЙ ЖЕ УЛИЦЕ — НЕ ОБРУБОК (ревью 19.09). У «Ленина 12»
    при записанном «ул Ленина 5, кв 12» слова улицы совпадают всегда, а номер
    дома легко совпадает с квартирой, этажом или подъездом — по числам это
    выглядело обрубком, и поправка клиента (или второй адрес) пропадала с
    экрана. Тот же дом той же улицы глушат `_то_же_место` и
    `_тот_же_дом_без_корпуса`; обрубок «27 мкр, 9» при «14 мкр, 27» остаётся
    поглощённым — у него другая «улица».
    """
    if row.conversation_id != источник.conversation_id:
        return False
    if row.kind == address_parse.KIND_HOUSE:
        ядро = address_parse.street_core(row.street)
        if (
            ядро
            and ядро == address_parse.street_core(источник.street)
            and geocode.house_key(row.house) != geocode.house_key(источник.house)
        ):
            return False
    числа = _числа_источника(источник)
    слова = _слова_источника(источник)
    своё = f"{row.street} {row.settlement or ''} {row.area or ''}"
    свои_числа = set(_ЧИСЛА.findall(f"{своё} {row.house}"))
    свои_слова = {w for w in _ЧТО_НЕ_ЧИСЛО.split(своё.lower()) if w and not w.isdigit()}
    if not свои_числа and not свои_слова:
        return False
    if not свои_числа <= числа:
        return False
    if row.kind == address_parse.KIND_PLACE:
        return свои_слова <= слова
    когда, когда_и = row.message_at or row.detected_at, источник.message_at or источник.detected_at
    if abs((когда - когда_и).total_seconds()) > 24 * 3600:
        return False
    return свои_слова <= слова


def _то_же_место(a: ClientAddressCandidate, b: ClientAddressCandidate) -> bool:
    """Одно место: слова одного адреса — или карта свела их в один дом.

    Карта главнее слов (ревью 13.09): «Ленина 5» → «улица Ленина, 5» и
    «проспект Ленина 5» → «проспект Ленина, 5» — обе подтверждены, дома
    разные, и словами их сводить нельзя.
    """
    if a.kind != b.kind:
        return False
    обе_подтверждены = a.geo_status == geocode.GEO_EXACT and b.geo_status == geocode.GEO_EXACT
    if обе_подтверждены and a.geo_formatted and b.geo_formatted:
        # Разные карты пишут один дом по-разному («Садовая улица, 18» у Яндекса,
        # «ул Садовая, 18» у DaData — бой 14.09, Хабаровск): сверяем ключом.
        return (
            a.geo_formatted == b.geo_formatted
            or _рядом(a, b)
            or geocode.formatted_key(a.geo_formatted) == geocode.formatted_key(b.geo_formatted)
        )
    if a.kind == address_parse.KIND_PLACE:
        # «13 микрорайон» старого разбора и «микрорайон 13» нового (владелец
        # 14.09, Ангарск) — одно место: слова те же, порядок не важен.
        return _ключ_места(a) == _ключ_места(b)
    # Пара «не обе exact» судится словами — и словами же о ПУНКТЕ (пакет 6.0а,
    # I-1): отказ человеком от «Лесная 15» (Ижевск) не запирает «Лесная
    # 15, Воткинск» — разные названные пункты, разные места. Названный у одной
    # стороны — не спор (`_другой_пункт`).
    return (
        a.kind == address_parse.KIND_HOUSE
        and address_parse.same_address(a.street, a.house, b.street, b.house)
        and not _другой_пункт(a, b)
    )


def _ключ_места(row: ClientAddressCandidate) -> tuple[frozenset[str], str, str]:
    слова = frozenset(
        geocode._ТИПЫ_УЛИЦ.get(w, w) for w in geocode._слова((row.value or "").replace(",", " "))
    )
    return (слова, (row.settlement or "").lower(), (row.locality or "").lower())


def _рядом(a: ClientAddressCandidate, b: ClientAddressCandidate) -> bool:
    """Две точки одного дома (~70 м, как `distinct_addresses`).

    Приблизительная точка (`~approx`: DaData при `qc_geo` 2/3 ставит одну
    точку на всю деревню или массив) — не улика одного дома (ревью 18.09):
    «ул Ленина 5» и «ул Пушкина 7» одной деревни стояли бы в одной точке.
    Остаются строка карты и её ключ.
    """
    if geocode.point_is_approx(a.geo_provider) or geocode.point_is_approx(b.geo_provider):
        return False
    if a.geo_lat is None or a.geo_lon is None or b.geo_lat is None or b.geo_lon is None:
        return False
    return (
        abs(a.geo_lat - b.geo_lat) <= geocode._ОДИН_ДОМ_ГРАДУСОВ
        and abs(a.geo_lon - b.geo_lon) <= geocode._ОДИН_ДОМ_ГРАДУСОВ
    )


def _строка_источник(
    client: Client, адреса: list[ClientAddressCandidate]
) -> ClientAddressCandidate | None:
    if client.address_candidate_id is None:
        return None
    return next((row for row in адреса if row.id == client.address_candidate_id), None)


async def identity_view(db: AsyncSession, client: Client) -> dict[str, Any]:
    """Личность клиента одним ответом: телефоны, идентификаторы, объединения.

    ЗАЧЕМ ОТДЕЛЬНАЯ РУЧКА, А НЕ ПОЛЯ В ДЕТАЛИ ДИАЛОГА. Ответ ключуется
    КЛИЕНТОМ, а не диалогом: у одного человека диалогов бывает девять, и
    складывать одинаковый список телефонов в каждый из них значило бы считать
    его девять раз и девять раз хранить в кэше. Плюс граница зон: деталь
    диалога собирает `app/services/conversations.py`, и лезть туда за
    личностью клиента — это связывать две вещи, которые меняются по разным
    поводам.

    ТЕЛЕФОНЫ И ИДЕНТИФИКАТОРЫ — СПИСКОМ. У одного человека их законно
    несколько: Авито выдаёт идентификатор мессенджера в пределах аккаунта, а
    аккаунтов у заказчика девять. Единственное поле `phone` в карточке
    означало бы «остальные номера мы забыли».

    СТЕПЕНЬ ДОВЕРИЯ К РУЧНОМУ ОБЪЕДИНЕНИЮ БЕРЁТСЯ ИЗ ЖУРНАЛА, А НЕ ИЗ ТЕКУЩИХ
    СТРОК. После объединения телефон победителя мог приехать от проигравшего —
    сравнение живых полей дало бы «телефоны совпали» всегда, то есть
    «подтверждено» у каждой ручной склейки, включая ошибочные. В журнале
    записано состояние ДО (`phone_match`), и врать ему нечем.

    ОТКУДА ВЗЯЛСЯ НОМЕР — ОТДЕЛЬНОЕ ПОЛЕ, А НЕ ДОГАДКА ЭКРАНА. Карточка
    подписывает телефон словами, и подпись обязана быть правдой: «введено
    вручную» под номером, вычитанным из переписки, значит, что карточка ручается
    за него чужим авторитетом, а «из диалога» под набранным со слов клиента —
    наоборот. Признака `phone_manual` для этого мало: он различает два случая
    из трёх (третий — номер, который принёс бот), и с появлением распознавания
    в тексте различать стало нужно четвёртый.
    """
    merged = await _merged_into_me(db, client.id)
    phone_match: dict[str, bool] = {}
    склейка: dict[str, dict[str, Any]] = {}
    for row in merged:
        audit_row = await last_merge_row(db, row.id)
        details = (audit_row.details or {}) if audit_row else {}
        phone_match[str(row.id)] = bool(details.get("phone_match"))
        склейка[str(row.id)] = details
    target = await db.get(Client, client.merged_into_id) if client.merged_into_id else None

    candidates = await phone_candidates(db, client.id)
    accepted = [c for c in candidates if c.status == CANDIDATE_ACCEPTED]

    # ДОКАЗАТЕЛЬСТВО К КАЖДОМУ НОМЕРУ (правка 12.09). Автоматика добавляет
    # дополнительные номера без вопроса, и оператор обязан видеть, откуда взялся
    # каждый: диалог, сообщение, дата, кто решил и слово рядом. Один и тот же
    # номер показывается один раз — основной побеждает (после склейки по
    # телефону он стоит у обеих карточек).
    строки_по_номеру: dict[str, ClientPhoneCandidate] = {}

    def из_переписки(row: ClientPhoneCandidate) -> bool:
        return row.client_id == client.id and row.source != CANDIDATE_SOURCE_SWAP

    for строка in accepted:  # старые первыми — среди равных побеждает первая
        прежняя = строки_по_номеру.get(строка.phone)
        if прежняя is None or (из_переписки(строка) and not из_переписки(прежняя)):
            строки_по_номеру[строка.phone] = строка

    def запись(value: str, owner: Client, *, primary: bool) -> dict[str, Any]:
        строка = строки_по_номеру.get(value)
        похож = False if primary else phone_rules.near_duplicate(client.phone, value)
        if строка is None:
            источник = "manual" if primary and owner.phone_set_at is not None else "other"
            return {
                "value": value,
                "client_id": str(owner.id),
                "primary": primary,
                "candidate_id": None,
                "source": источник,
                "conversation_id": None,
                "message_id": None,
                "message_at": None,
                "hint": None,
                "decided_by": None,
                "near_primary": похож,
            }
        if строка.source == CANDIDATE_SOURCE_SWAP:
            источник = "swap"
        elif primary and owner.phone_set_at is not None:
            источник = "manual"
        elif строка.source == CANDIDATE_SOURCE_VOICE:
            # Тот же порядок, что в `_phone_source`: голос после ручного
            # ввода и перед «из диалога» — подпись списка и подпись основного
            # не расходятся.
            источник = "voice"
        else:
            источник = "dialog"
        return {
            "value": value,
            "client_id": str(owner.id),
            "primary": primary,
            "candidate_id": str(строка.id),
            "source": источник,
            "conversation_id": str(строка.conversation_id) if строка.conversation_id else None,
            "message_id": str(строка.message_id) if строка.message_id else None,
            "message_at": строка.message_at.isoformat() if строка.message_at else None,
            "hint": строка.hint,
            "decided_by": "auto" if строка.resolved_by_id is None else "operator",
            "near_primary": похож,
        }

    phones: list[dict[str, Any]] = []
    known: set[str] = set()
    for c in (client, *merged):
        # После склейки по телефону номер стоит у обеих карточек — строка одна.
        if c.phone and c.phone not in known:
            known.add(c.phone)
            phones.append(запись(c.phone, c, primary=c.id == client.id))
    # Подтверждённые кандидаты — тоже телефоны этого человека, просто не
    # основные. Не показать их значило бы потерять ровно то, ради чего оператор
    # нажимал «добавить» (или автоматика записала сама).
    for строка in accepted:
        if строка.phone in known:
            continue
        known.add(строка.phone)
        owner = (
            client
            if строка.client_id == client.id
            else next((c for c in merged if c.id == строка.client_id), client)
        )
        phones.append(запись(строка.phone, owner, primary=False))
    avito_ids = [
        {"value": c.external_id, "client_id": str(c.id), "primary": c.id == client.id}
        for c in (client, *merged)
    ]
    адреса = await address_candidates(db, client.id)
    источник = _строка_источник(client, адреса)
    уровни = set(str(await app_settings.get(db, app_settings.ADDRESS_DETECT_LEVELS) or ""))
    # Карта выключена — под предложениями не должно вечно висеть «проверяем»:
    # строки остаются `pending` (включат — проверятся), а экрану отдаём «карта
    # не спрашивалась» пустым вердиктом.
    карта_включена = bool(await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED))
    autofill = bool(await app_settings.get(db, app_settings.ADDRESS_DETECT_AUTOFILL))
    rule_policy = geocode.parse_rule_policy(
        str(await app_settings.get(db, app_settings.ADDRESS_GEO_RULE_POLICY) or ""), strict=False
    )
    return {
        "id": str(client.id),
        "name": client.name,
        "phone": client.phone,
        "phone_manual": client.phone_set_at is not None,
        # `manual` — набрали руками, `dialog` — вычитали из переписки,
        # `other` — номер есть, но происхождение не наше (бот, строки старше
        # этой правки), `none` — номера нет. Подпись под номером в карточке
        # берётся отсюда: врать про происхождение нельзя ни в одну сторону.
        "phone_source": _phone_source(client, accepted),
        # Распознанные номера, ЖДУЩИЕ решения оператора: «заменить»,
        # «добавить», «отклонить». Пустой список — спрашивать не о чем.
        "phone_candidates": [
            candidate_view(row) for row in candidates if row.status == CANDIDATE_PENDING
        ],
        # --- адрес выезда ---------------------------------------------------
        "address": client.address,
        # Подпись под адресом — по тому же правилу, что у телефона: врать про
        # происхождение нельзя ни в одну сторону.
        "address_source": _address_source(client, адреса),
        # ⚠ ПОКАЗЫВАЕМ НЕ ВСЁ РАСПОЗНАННОЕ, А ТОЛЬКО РАЗРЕШЁННЫЕ УРОВНИ.
        # Уровень A («ул.», «дом 12» названы прямо) на бою дал 3 613 находок в
        # 3 355 диалогах; уровень C (широкий невод без контекста) — 2 619, и
        # половина из них ложные по замеру источника. Записываются все, чтобы
        # можно было мерить качество, а показывается то, что назвал
        # руководитель (`ADDRESS_DETECT_LEVELS`, умолчание «A»).
        # С 18.09 это ПОКАЗ, А НЕ ВОПРОС: в списке — строки, которые автоматика
        # НЕ записала (ещё проверяются, удержаны сторожем или второй адрес
        # диалога); записывает автоматика сама по степени `candidate_grade`.
        # С экрана — «Не адрес», а где автоматика сама не запишет (`writable`),
        # ещё «Записать в карточку».
        # Уровень ниже разрешённого всё же показывается, когда КАРТА нашла дом:
        # «Липовая 6» без «ул.» — уровень C, но три Липовых, 6 по области
        # (бой 12.09, Череповец) — это адрес, и оператору есть из чего выбрать.
        # …но варианты по области у невода C — только с той же улицей словом
        # (стенд 14.09: «Просто 3» → «ГСК Простор, 3, Норильск» — совпадение
        # по основе с допуском, не улица).
        "address_candidates": [
            {
                **address_candidate_view(row),
                "parts": части,
                "geo": _geo_на_экран(row, карта_включена),
                # Записать в карточку одним нажатием (`_operator_writes`).
                "writable": _operator_writes(row, autofill=autofill, policy=rule_policy),
            }
            for row, части in _без_дублей(
                [
                    row
                    for row in адреса
                    if row.status == CANDIDATE_PENDING
                    and (row.level in уровни or _карта_нашла_дом(row))
                ],
                источник,
            )
        ],
        # Вердикт карты и цитата клиента ПОД УЖЕ ЗАПИСАННЫМ адресом: до 11.09
        # цитата исчезала вместе с принятием строки, а доказательство обязано
        # жить дольше решения — по нему мастер сверяет, туда ли едет.
        "address_geo": _geo_на_экран(источник, карта_включена) if источник is not None else None,
        "address_evidence": (
            {
                "raw": источник.raw,
                "parts": {
                    ч: getattr(источник, ч)
                    for ч in _ЧАСТИ_АДРЕСА
                    if getattr(источник, ч) is not None
                },
                "conversation_id": str(источник.conversation_id),
                "locality": источник.locality,
            }
            if источник is not None
            else None
        ),
        # Подтверждённые адреса этого человека, кроме основного: у него законно
        # два (свой и мамин). Основной узнаётся по связи, а для строк до 11.09
        # — по тексту.
        "addresses": [
            {"value": row.geo_formatted or row.value, "client_id": str(row.client_id)}
            for row in адреса
            if row.status == CANDIDATE_ACCEPTED
            and row.id != client.address_candidate_id
            # Сравнение только с непустыми: при пустой карточке `None not in
            # (value, None)` давало False и прятало принятые строки (ревью 11.09).
            and (
                client.address is None
                or client.address not in {v for v in (row.value, row.geo_formatted) if v}
            )
        ],
        "external_id": client.external_id,
        "phones": phones,
        # Снятые «Не его номер» — с кнопкой «Вернуть» (12.09): снятие обратимо.
        # Только строки ЭТОЙ карточки: у присоединённых свои решения.
        "rejected_phones": [
            {
                "value": row.phone,
                "candidate_id": str(row.id),
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            }
            for row in candidates
            if row.status == CANDIDATE_REJECTED
            and row.client_id == client.id
            and row.phone != client.phone
        ],
        "avito_ids": avito_ids,
        "merged_from": [
            {
                **short_view(row),
                "merged_at": row.merged_at.isoformat() if row.merged_at else None,
                # «подтверждено» — телефоны обеих карточек совпали ДО
                # объединения; «предположительно» — совпало что-то слабее
                # (имя, идентификатор) либо телефона не было вовсе.
                "confidence": "confirmed" if phone_match.get(str(row.id)) else "assumed",
                # Объединила автоматика — экран говорит это прямо и называет
                # правило; у ручного объединения полей нет.
                "auto": bool(склейка.get(str(row.id), {}).get("auto")),
                "rule": склейка.get(str(row.id), {}).get("rule"),
            }
            for row in merged
        ],
        # Карточка, В КОТОРУЮ увели эту. Сегодня карточка открывается только
        # из диалога, а диалоги при объединении переезжают к победителю, — то
        # есть в интерфейсе это поле почти всегда пусто. Почти: новый чат на
        # проигравший идентификатор Авито заведёт диалог под ней (см.
        # `app/models/client.py`, раздел про шов), и тогда оператор обязан
        # увидеть, что открыл «хвост» объединённой карточки, а не новую.
        "merged_into": short_view(target) if target else None,
    }


async def vetoed_pairs(db: AsyncSession, client_id: uuid.UUID) -> set[uuid.UUID]:
    """Карточки, с которыми эту разъединял человек (`client_merge_vetoes`)."""
    rows = (
        await db.execute(
            sa.select(ClientMergeVeto.a_id, ClientMergeVeto.b_id).where(
                sa.or_(ClientMergeVeto.a_id == client_id, ClientMergeVeto.b_id == client_id)
            )
        )
    ).all()
    return {b if a == client_id else a for a, b in rows}


async def merge_candidates(db: AsyncSession, client: Client) -> list[dict[str, Any]]:
    """Карточки, которые МОЖЕТ БЫТЬ описывают того же человека.

    Это подсказка, а не решение: каждый пункт показывается кнопкой
    «Объединить», и ни один не применяется сам. Причина «почему предложено»
    едет вместе с пунктом — без неё оператор не отличит совпавший телефон
    (сильный довод) от совпавшего имени (в ремонте техники «Иван» — это
    каждый десятый).

    ПОЧЕМУ СОВПАДЕНИЕ ИДЕНТИФИКАТОРА СЕГОДНЯ НЕ СРАБАТЫВАЕТ ВНУТРИ АВИТО.
    `UNIQUE(channel, external_id)` не даёт двум карточкам одного канала
    получить один идентификатор — они бы просто не завелись раздельно. Правило
    оставлено рабочим, потому что канал в схеме не один: у второго канала
    (когда он появится) идентификаторы свои, и совпадение станет возможным.
    """
    out: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    # Пары, которые человек разъединял: подсказка остаётся, но говорит об этом
    # прямо — автоматика такую пару больше не тронет, а оператор пусть знает,
    # что коллега уже решал.
    разъединяли = await vetoed_pairs(db, client.id)

    def add(row: Client, reason: str, confidence: str) -> None:
        if row.id in seen or row.id == client.id or row.merged_into_id is not None:
            return
        seen.add(row.id)
        out.append(
            {
                **short_view(row),
                "reason": reason,
                "confidence": confidence,
                "vetoed": row.id in разъединяли,
            }
        )

    if client.phone:
        for row in await phone_twins(db, client.phone, exclude_id=client.id):
            add(row, "phone", "confirmed")

    # РАСПОЗНАННЫЙ В ПЕРЕПИСКЕ НОМЕР — ТОЛЬКО ПОДСКАЗКА, И ЭТО ПРЯМОЕ ТРЕБОВАНИЕ
    # ВЛАДЕЛЬЦА: «по распознанному номеру НЕ запускаем автообъединение карточек
    # — только предложение в блоке „Возможно, это тот же человек“».
    #
    # Мера доверия НИЖЕ, чем у совпавшего телефона карточки, и это не
    # осторожность ради осторожности: там номер уже подтверждён человеком (его
    # ввели руками или согласились с ним), здесь — цепочка цифр, которую никто
    # не проверял. Выдать её за подтверждение значило бы предложить объединение
    # с той же уверенностью, с какой 11 августа под одним именем собрались
    # восемь человек из разных городов.
    #
    # Отклонённые кандидаты не участвуют: оператор уже сказал, что это не его
    # телефон, и возвращать предложение по нему — способ научить людей не читать
    # подсказки.
    for candidate in await phone_candidates(db, client.id):
        if candidate.status == CANDIDATE_REJECTED or candidate.phone == client.phone:
            continue
        for row in await phone_twins(db, candidate.phone, exclude_id=client.id):
            add(row, "phone_candidate", "assumed")

    for row in (
        (
            await db.execute(
                sa.select(Client).where(
                    Client.external_id == client.external_id,
                    Client.id != client.id,
                    Client.merged_into_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    ):
        add(row, "external_id", "assumed")

    # Имя — самый слабый довод, поэтому и последний: совпадение без телефона
    # ровно та ошибка, из-за которой 11 августа под одним именем собрались
    # восемь человек из разных городов. Пустое и слишком короткое имя не ищем
    # вовсе: «Ак» или «-» склеили бы посторонних.
    # ⚠ ТОЛЬКО РЕДКОЕ ПОЛНОЕ ИМЯ (просьба владельца 12.09: «у нас больше 34
    # аккаунтов, связывать людей только по имени — точность почти минимальная,
    # совпадения есть только по уникальным именам»). «Леся» и «Иван» — не
    # довод: одно слово носят тысячи. Два слова и не больше двух тёзок во
    # всей базе — тогда это, скорее всего, тот же человек в другом объявлении.
    name = " ".join((client.name or "").split())
    if len(name) >= 6 and len(name.split()) >= 2:
        namesakes = (
            (
                await db.execute(
                    sa.select(Client).where(
                        sa.func.lower(Client.name) == name.lower(),
                        Client.id != client.id,
                        Client.merged_into_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        # 17.08, жалоба владельца на спам: карточка предлагала ВСЕХ тёзок,
        # включая пустышки без телефона — три «Ольги» подряд, а с загрузкой
        # истории их стали бы десятки. Тёзка — довод только когда он редкий
        # (само их количество говорит «это каждый десятый Иван») и когда у
        # кандидата есть телефон: объединение с бестелефонной пустышкой не
        # даёт оператору ничего.
        with_phone = [row for row in namesakes if (row.phone or "").strip()]
        if 0 < len(with_phone) <= 2:
            for row in with_phone:
                add(row, "name", "assumed")
    out = out[:5]
    # В карточку, в которую уже объединяли, другую присоединять нельзя (цепочка
    # сломала бы «Разъединить») — объединять надо в обратную сторону: эту в неё.
    # Экрану нужен признак, иначе кнопка всегда получала 422.
    group_roots = set(
        (
            await db.execute(
                sa.select(Client.merged_into_id)
                .where(Client.merged_into_id.in_([client.id, *(uuid.UUID(r["id"]) for r in out)]))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    for entry in out:
        entry["has_group"] = uuid.UUID(entry["id"]) in group_roots
        entry["mine_has_group"] = client.id in group_roots
    return out
