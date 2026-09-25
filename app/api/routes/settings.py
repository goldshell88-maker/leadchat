"""Настройки работы команды (`/settings/*`).

Ручка на ГРУППУ настроек, а не на переключатель: экран читает и пишет их
пачкой, и отдельный маршрут под каждую галку плодил бы маршруты со скоростью
появления настроек. Групп сегодня три — автораздача, рабочие часы статистики и
распознавание телефонов, — и делятся они по экрану, на котором живут вместе.
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.core import trace
from app.core.errors import ApiError
from app.integrations import gateway, openrouter
from app.models import ClientAddressCandidate, User
from app.models.api_registry import ApiRegistryEntry
from app.services import (
    address_catchup,
    address_parse,
    api_monitor,
    app_settings,
    geocode,
    phone_rules,
)
from app.services import audit as audit_svc
from app.services import clients as clients_svc
from app.services.geocode_queue import enqueue_autofill_catchup

log = structlog.get_logger("app.settings")

router = APIRouter()

# Включение автораздачи меняет работу всей смены сразу, поэтому право отдельное
# от `users:manage`: заводить сотрудников и решать, как между ними раздаются
# диалоги, — разные полномочия.
settings_perm = require_permission("settings:manage")


class DistributionPatch(BaseModel):
    """Тело `PATCH /settings/distribution`.

    Все поля необязательны: экран шлёт только то, что человек менял. Но
    `max_active` обязан различать «не трогали» и «сняли ограничение» — второе
    это `null`, законное значение. Поэтому у поля отдельный признак, а не
    привычное `None = не задано`.
    """

    enabled: bool | None = None
    max_active: int | None = Field(default=None, ge=1, le=app_settings.MAX_ACTIVE_LIMIT)
    #: `true` — снять ограничение. Разводит `null` («без потолка») и отсутствие
    #: поля («не меняли»), которые в JSON выглядят одинаково.
    max_active_unlimited: bool = False
    #: Освобождать ли диалоги сотрудника, недоступного дольше пятнадцати минут
    #: (сторож `scheduler/jobs/reclaim.release_unavailable_owners`). В ЭТОЙ
    #: ручке, а не в своей: это третье решение о том, у кого лежит диалог, и
    #: живёт оно на том же экране под тем же правом.
    release_unavailable: bool | None = None
    #: Кого сторож освобождения не трогает. Присылается ЦЕЛИКОМ, а не дельтой:
    #: список короткий, а «прислать разницу» потребовало бы решать, что делать
    #: с одновременной правкой двумя администраторами. Пустой список —
    #: законное значение («исключений нет»), поэтому `None` значит «не меняли».
    release_exempt_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)


def _view(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": values[app_settings.DISTRIBUTION_ENABLED],
        "max_active": values[app_settings.DISTRIBUTION_MAX_ACTIVE],
        "release_unavailable": values[app_settings.RELEASE_UNAVAILABLE_ENABLED],
        # Список отдаётся РАЗОБРАННЫМ, а не строкой: разбирать его на фронте
        # значило бы завести второй разбор той же строки — см. довод у
        # `parse_exempt_ids`.
        "release_exempt_ids": sorted(
            str(u)
            for u in app_settings.parse_exempt_ids(values[app_settings.RELEASE_UNAVAILABLE_EXEMPT])
        ),
    }


@router.get("/settings/distribution")
async def get_distribution_settings(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return _view(await app_settings.get_all(db))


@router.patch("/settings/distribution")
async def patch_distribution_settings(
    body: DistributionPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Изменить настройки автораздачи и сторожа освобождения диалогов.

    Пишется вся пачка целиком или ничего: человек, включивший раздачу и
    получивший отказ на потолке, будет уверен, что не сработало ничего, — а
    раздача уже пошла.

    `release_unavailable` — выключатель сторожа, который забирает диалоги у
    недоступного дольше пятнадцати минут (просьба владельца 06.09). Он в этой
    же пачке и в этом же журнале: вопрос «почему диалоги ушедших со вчера висят
    за ними» — того же рода, что «почему со вчера не раздаются».
    """
    before = _view(await app_settings.get_all(db))

    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch[app_settings.DISTRIBUTION_ENABLED] = body.enabled
    if body.max_active_unlimited:
        patch[app_settings.DISTRIBUTION_MAX_ACTIVE] = None
    elif body.max_active is not None:
        patch[app_settings.DISTRIBUTION_MAX_ACTIVE] = body.max_active
    if body.release_unavailable is not None:
        patch[app_settings.RELEASE_UNAVAILABLE_ENABLED] = body.release_unavailable
    if body.release_exempt_ids is not None:
        # Храним отсортированным: тогда «было ≠ стало» в журнале означает
        # настоящую перемену состава, а не другой порядок тех же людей.
        patch[app_settings.RELEASE_UNAVAILABLE_EXEMPT] = ", ".join(
            sorted(str(u) for u in set(body.release_exempt_ids))
        )

    if not patch:
        return before

    after = _view(await app_settings.set_many(db, patch, user_id=user.id))
    if after != before:
        # В журнал уходит и «было», и «стало»: вопрос «почему со вчера диалоги
        # не раздаются» обязан иметь ответ с именем и временем.
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.distribution_changed",
            entity="settings",
            details={"before": before, "after": after},
        )
    await db.commit()
    return after


# --- рабочие часы статистики (#41) --------------------------------------------


class WorkHoursPatch(BaseModel):
    """Тело `PATCH /settings/work-hours`. Оба поля необязательны."""

    start_hour: int | None = Field(default=None, ge=0, le=23)
    # 24 у конца — «до конца суток»: без него максимум окна был 0–23, то есть
    # двадцать три часа, и круглосуточно не выставлялось вовсе.
    end_hour: int | None = Field(default=None, ge=0, le=24)


def _hours_view(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_hour": values[app_settings.STATS_WORK_START_HOUR],
        "end_hour": values[app_settings.STATS_WORK_END_HOUR],
    }


@router.get("/settings/work-hours")
async def get_work_hours(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return _hours_view(await app_settings.get_all(db))


@router.patch("/settings/work-hours")
async def patch_work_hours(
    body: WorkHoursPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Окно, по которому считается «скорость первого ответа в рабочие часы».

    ЗАЧЕМ ЭТО ВООБЩЕ НАСТРОЙКА. Часы были зашиты в код дважды, и поменять их
    можно было только правкой файлов с перезапуском — то есть через инженера,
    хотя решение управленческое: меняются смены, меняется окно.

    ЦИФРЫ МЕНЯЮТСЯ НЕ СРАЗУ, и это надо понимать. Витрина статистики
    пересчитывается раз в час; до ближайшего пересчёта отчёты показывают
    прежние значения. Ускорять это здесь нельзя: пересчёт на боевом объёме —
    минуты, и запускать его нажатием кнопки в настройках значит подвесить
    экран человеку, который просто поправил час.

    Порядок концов НЕ проверяется намеренно: настройки меняют по одной, и
    запрет «начало позже конца» не дал бы поменять пару 10–20 на 8–22 вовсе —
    первое же сохранение упёрлось бы в себя.
    """
    before = _hours_view(await app_settings.get_all(db))

    patch: dict[str, Any] = {}
    if body.start_hour is not None:
        patch[app_settings.STATS_WORK_START_HOUR] = body.start_hour
    if body.end_hour is not None:
        patch[app_settings.STATS_WORK_END_HOUR] = body.end_hour
    if not patch:
        return before

    after = _hours_view(await app_settings.set_many(db, patch, user_id=user.id))
    if after != before:
        # Отчёты после этой правки читаются иначе, а выглядят так же. Без
        # записи в журнал вопрос «почему средняя скорость ответа выросла вдвое»
        # остался бы без ответа.
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.work_hours_changed",
            entity="settings",
            details={"before": before, "after": after},
        )
    await db.commit()
    return after


# --- распознавание телефонов в тексте (правка 10 от 12 августа) ---------------


class PhoneDetectPatch(BaseModel):
    """Тело `PATCH /settings/phone-detect`. Оба поля необязательны.

    `None` здесь означает ровно «не трогали»: у обоих переключателей нет
    третьего состояния, поэтому отдельный признак «сбросить», как у потолка
    автораздачи, не нужен и завёл бы лишнее понятие.
    """

    enabled: bool | None = None
    autofill: bool | None = None
    #: Наши номера через запятую (12.09) — в карточку клиента не пишутся.
    own_numbers: str | None = Field(default=None, max_length=2000)
    #: Автообъединение двойников по телефону: off / shadow / on (12.09).
    merge_auto: Literal["off", "shadow", "on"] | None = None


def _phone_detect_view(values: dict[str, Any]) -> dict[str, Any]:
    own = phone_rules.parse_own_numbers(values.get(app_settings.PHONE_OWN_NUMBERS))
    return {
        "enabled": values[app_settings.PHONE_DETECT_ENABLED],
        "autofill": values[app_settings.PHONE_DETECT_AUTOFILL],
        "own_numbers": values.get(app_settings.PHONE_OWN_NUMBERS) or "",
        # Экран показывает, сколько номеров разобралось из строки: строка с
        # мусором молча теряла бы номера, и человек узнал бы об этом по факту.
        "own_numbers_parsed": sorted(own),
        "merge_auto": values.get(app_settings.CLIENT_MERGE_AUTO) or "off",
    }


@router.get("/settings/phone-detect")
async def get_phone_detect(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return _phone_detect_view(await app_settings.get_all(db))


@router.patch("/settings/phone-detect")
async def patch_phone_detect(
    body: PhoneDetectPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Два переключателя распознавания номеров в тексте входящих.

    `enabled` — разбирать ли текст вообще. `autofill` — писать ли найденное
    прямо в пустую карточку (иначе номер ложится предложением оператору,
    `client_phone_candidates`). Непустая карточка не перезаписывается ни при
    каком сочетании — это правило живёт в разборе входящих, а не здесь, и
    выключателя у него нет.

    ПОЧЕМУ ОБА В ОДНОЙ РУЧКЕ, А НЕ ПОРОЗНЬ. Их читают и показывают вместе:
    «распознавать» без «писать сразу» — рабочее сочетание, «писать сразу» без
    «распознавать» — бессмыслица, и увидеть это человек должен на одном экране,
    а не после сохранения.

    ВТОРОЙ ПЕРЕКЛЮЧАТЕЛЬ ОПАСНЕЕ ПЕРВОГО, и порядок в журнале это покажет:
    включённый `autofill` означает, что цепочка цифр из чужого сообщения молча
    становится телефоном в карточке, а по нему звонят и его диктуют мастеру.
    Ошибка стоит звонка постороннему человеку.
    """
    before = _phone_detect_view(await app_settings.get_all(db))

    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch[app_settings.PHONE_DETECT_ENABLED] = body.enabled
    if body.autofill is not None:
        patch[app_settings.PHONE_DETECT_AUTOFILL] = body.autofill
    if body.own_numbers is not None:
        patch[app_settings.PHONE_OWN_NUMBERS] = body.own_numbers.strip()
    if body.merge_auto is not None:
        patch[app_settings.CLIENT_MERGE_AUTO] = body.merge_auto
    if not patch:
        return before

    after = _phone_detect_view(await app_settings.set_many(db, patch, user_id=user.id))
    if after != before:
        # Настройка решает за клиента, что вот эта цепочка цифр — его телефон.
        # Вопрос «с какого числа в карточки поехали номера из переписки»
        # обязан иметь ответ с именем и временем.
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.phone_detect_changed",
            entity="settings",
            details={"before": before, "after": after},
        )
    await db.commit()
    return after


# --- адрес из переписки и проверка по карте (11.09) ---------------------------


class AddressDetectPatch(BaseModel):
    """Тело `PATCH /settings/address-detect`. Все поля необязательны."""

    enabled: bool | None = None
    autofill: bool | None = None
    #: Буквы уровней показа: «A», «AB», «ABC», пусто — не показывать.
    levels: str | None = Field(default=None, max_length=3, pattern=r"^[ABC]*$")
    geo_enabled: bool | None = None
    provider: Literal["nominatim", "yandex", "osm_then_yandex"] | None = None
    #: Потолок запросов к Яндексу в сутки (бесплатный тариф — 1000). Пусто —
    #: без потолка; это только для платного тарифа.
    yandex_daily_limit: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Явное «снять потолок»: `None` в теле значит «не трогали».
    unlimited_yandex: bool | None = None
    suggest_enabled: bool | None = None
    dadata_enabled: bool | None = None
    llm_enabled: bool | None = None
    ahunter_enabled: bool | None = None
    speller_enabled: bool | None = None
    #: Автопривязка адреса без человека (18.09): политика карты довершает
    #: вердикт до степени — точная/приблизительная точка или строка улицы.
    auto_decide: bool | None = None
    #: Политика правил автопривязки и разбора (пакет 6.0а, §0.3): строки
    #: «имя=значение,…». Имена и значения проверяет реестр настроек
    #: (`app_settings.Spec.check`) — неизвестное имя → 400, в таблицу не
    #: попадает. Пустая строка — снять все перекрытия (одни умолчания).
    rule_policy: str | None = Field(default=None, max_length=2000)
    parse_rules: str | None = Field(default=None, max_length=2000)
    #: Свои адреса (пакет 7а, Q24): «улица дом» через запятую, как свои
    #: номера у телефона; каждую часть проверяет реестр настроек
    #: (`address_own.check_list`) — мусор → 400 с текстом части. Список,
    #: выведенный задачей (`own_addresses_auto`), в теле не принимается.
    own_addresses: str | None = Field(default=None, max_length=2000)
    #: Один вопрос об адресе клиенту (18.09). Границы задержки и порога —
    #: здесь, а не в реестре: `count_or_null` знает только 0..1 000 000.
    ask_enabled: bool | None = None
    ask_delay_sec: int | None = Field(
        default=None,
        ge=app_settings.ADDRESS_ASK_DELAY_MIN_SEC,
        le=app_settings.ADDRESS_ASK_DELAY_MAX_SEC,
    )
    #: Текст вопроса; проверяется сторожем слов об адресе до записи. ≤1000 —
    #: одна часть доставки Авито.
    ask_text: str | None = Field(default=None, min_length=1, max_length=1000)
    ask_min_chars: int | None = Field(default=None, ge=0, le=app_settings.ADDRESS_ASK_MIN_CHARS_MAX)


def _address_detect_view(
    values: dict[str, Any],
    *,
    yandex_used_today: int = 0,
    suggest_used_today: int = 0,
    dadata_used_today: int = 0,
    llm_used_today: int = 0,
    ahunter_used_today: int = 0,
    speller_used_today: int = 0,
) -> dict[str, Any]:
    return {
        "enabled": values[app_settings.ADDRESS_DETECT_ENABLED],
        "autofill": values[app_settings.ADDRESS_DETECT_AUTOFILL],
        "levels": values[app_settings.ADDRESS_DETECT_LEVELS],
        "geo_enabled": values[app_settings.ADDRESS_GEO_ENABLED],
        "provider": values[app_settings.ADDRESS_GEO_PROVIDER],
        # Ключи провайдеров лежат на шлюзе Амстердама (docs/46); экрану нужно
        # знать лишь, есть ли ключ по снимку `/status`, — иначе переключение
        # на «yandex» молча даст `blocked`. Шлюз не настроен — ключей нет, и
        # подпись называет это отдельно (`gateway_configured`, проверка 24.09):
        # «нет ключа у шлюза» и «шлюза нет» чинятся в разных местах.
        "gateway_configured": gateway.enabled(),
        "yandex_key_present": gateway.enabled() and gateway.key_present("yandex_geocoder"),
        "yandex_daily_limit": values[app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT],
        # Сколько уже спросили у Яндекса сегодня: владелец платит за каждую
        # тысячу сверх бесплатной, и расход обязан быть виден там же, где
        # выключатель.
        "yandex_used_today": yandex_used_today,
        # Доля Яндекса и Саджеста для починки, процентов (пакет 5) — ТОЛЬКО НА
        # ЧТЕНИЕ: экрану видно, что получает хвост; правится из консоли
        # (`address-limits --repair-share-yandex`), как потолок DaData.
        "repair_share_yandex": app_settings.repair_share_pct(
            values[app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX]
        ),
        "suggest_enabled": values[app_settings.ADDRESS_GEO_SUGGEST_ENABLED],
        "suggest_key_present": gateway.enabled() and gateway.key_present("yandex_suggest"),
        "suggest_daily_limit": values[app_settings.ADDRESS_GEO_SUGGEST_DAILY_LIMIT],
        "suggest_used_today": suggest_used_today,
        # DaData спрашивается первой (ФИАС с координатами); без ключа на
        # шлюзе переключатель на экране бесполезен — экрану нужно это знать.
        "dadata_enabled": values[app_settings.ADDRESS_GEO_DADATA_ENABLED],
        "dadata_key_present": gateway.enabled() and gateway.key_present("dadata"),
        "dadata_daily_limit": values[app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT],
        "dadata_used_today": dadata_used_today,
        # Модель как второй читатель (13.09): без ключа читателя на шлюзе
        # переключатель бесполезен — экрану нужно это знать.
        "llm_enabled": values[app_settings.ADDRESS_LLM_ENABLED],
        # Любой из читателей с ключом (OpenRouter, Groq, Mistral — 16.09).
        "llm_key_present": openrouter.enabled(),
        "llm_daily_limit": values[app_settings.ADDRESS_LLM_DAILY_LIMIT],
        "llm_used_today": llm_used_today,
        # Ahunter — второй ГАР без ключа и потолка; Спеллер — опечатки в
        # улице, 10 000 в сутки (16.09). Ключей нет — экрану нужны только
        # переключатели и расход.
        "ahunter_enabled": values[app_settings.ADDRESS_GEO_AHUNTER_ENABLED],
        "ahunter_used_today": ahunter_used_today,
        "speller_enabled": values[app_settings.ADDRESS_GEO_SPELLER_ENABLED],
        "speller_daily_limit": values[app_settings.ADDRESS_GEO_SPELLER_DAILY_LIMIT],
        "speller_used_today": speller_used_today,
        # Автопривязка и вопрос клиенту (18.09). Отдаются ХРАНИМЫЕ значения,
        # как у соседей, а не «работает ли» (ask_enabled AND enabled):
        # иначе при выключенном разборе PATCH записывался бы, а тумблер на
        # экране «не держался». Зависимость экран знает сам.
        "auto_decide": values[app_settings.ADDRESS_GEO_AUTO_DECIDE],
        # Политики правил (пакет 6.0а) — явно в блоке «Адреса в переписке»:
        # строка настройки как есть плюс действующая политика каждого правила
        # (умолчания реестра под перекрытиями) и подписи — экрану не нужно
        # знать реестр, чтобы показать лестницу по правилам.
        "rule_policy": values[app_settings.ADDRESS_GEO_RULE_POLICY],
        "rule_policy_effective": _rule_policy_effective(
            values[app_settings.ADDRESS_GEO_RULE_POLICY]
        ),
        "parse_rules": values[app_settings.ADDRESS_PARSE_RULES],
        # Действующее состояние каждого правила разбора с подписью (проверка
        # 24.09) — как у политик правил выше: экран показывает, что включено,
        # не зная реестра.
        "parse_rules_effective": _parse_rules_effective(values[app_settings.ADDRESS_PARSE_RULES]),
        # Свои адреса (Q24): список владельца — правится; выведенный задачей —
        # только показ, чтобы человек видел, что автоматика считает адресом
        # компании, и мог перекрыть своим списком или снять правило.
        "own_addresses": values.get(app_settings.ADDRESS_OWN_ADDRESSES) or "",
        "own_addresses_auto": values.get(app_settings.ADDRESS_OWN_ADDRESSES_AUTO) or "",
        "ask_enabled": values[app_settings.ADDRESS_ASK_ENABLED],
        "ask_delay_sec": values[app_settings.ADDRESS_ASK_DELAY_SEC],
        "ask_text": values[app_settings.ADDRESS_ASK_TEXT],
        "ask_min_chars": values[app_settings.ADDRESS_ASK_MIN_CHARS],
    }


def _rule_policy_effective(text: str) -> list[dict[str, str]]:
    """Действующая политика по каждому правилу реестра: имя, подпись,
    политика (перекрытие настройки или умолчание). Чтение нестрогое, как у
    воркера и задачи: имя, снятое из реестра после записи, пропускается, а
    остальные перекрытия на экране те же, что в бою."""
    перекрытия = geocode.parse_rule_policy(text, strict=False)
    return [
        {
            "rule": rule,
            "label": geocode.RULE_LABEL.get(rule, rule),
            "policy": geocode.rule_policy(перекрытия, rule),
        }
        for rule in geocode.RULE_DEFAULT_POLICY
    ]


def _parse_rules_effective(text: str) -> list[dict[str, str]]:
    """Состояние каждого правила разбора: имя, подпись, `on`/`off`/`shadow`
    (перекрытие настройки или умолчание реестра). Чтение нестрогое, как у
    приёма входящих: пара с именем, снятым из реестра, пропускается."""
    states = address_parse.rules_from_setting(text, strict=False)
    return [
        {
            "rule": rule,
            "label": address_parse.PARSE_RULE_LABEL.get(rule, rule),
            "state": states[rule],
        }
        for rule in address_parse.PARSE_RULES
    ]


async def _yandex_used_today(redis: Redis) -> int:
    from app.workers.geocode import yandex_calls_today

    return await yandex_calls_today(redis)


async def _suggest_used_today(redis: Redis) -> int:
    from app.workers.geocode import suggest_calls_today

    return await suggest_calls_today(redis)


async def _dadata_used_today(redis: Redis) -> int:
    from app.workers.geocode import dadata_calls_today

    return await dadata_calls_today(redis)


async def _llm_used_today(redis: Redis) -> int:
    from app.workers.address_llm import llm_calls_today

    return await llm_calls_today(redis)


async def _ahunter_used_today(redis: Redis) -> int:
    from app.workers.geocode import ahunter_calls_today

    return await ahunter_calls_today(redis)


async def _speller_used_today(redis: Redis) -> int:
    from app.workers.geocode import speller_calls_today

    return await speller_calls_today(redis)


def _looks_like_address_question(text: str) -> bool:
    """Ответ на этот текст разбор прочитает как адрес: правило то же, по
    которому `inbound._оператор_спросил_адрес` узнаёт вопрос оператора, —
    одно на ручку, отправителя вопроса и разбор ответа (прецедент чтения
    приватного имени — `cli.py`)."""
    from app.services import inbound

    return bool(text) and inbound._ВОПРОС_ОБ_АДРЕСЕ.search(text) is not None


@router.get("/settings/address-detect")
async def get_address_detect(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    return _address_detect_view(
        await app_settings.get_all(db),
        yandex_used_today=await _yandex_used_today(redis),
        suggest_used_today=await _suggest_used_today(redis),
        dadata_used_today=await _dadata_used_today(redis),
        llm_used_today=await _llm_used_today(redis),
        ahunter_used_today=await _ahunter_used_today(redis),
        speller_used_today=await _speller_used_today(redis),
    )


@router.patch("/settings/address-detect")
async def patch_address_detect(
    body: AddressDetectPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Переключатели адреса — на одном экране, как у телефона.

    `enabled` — разбирать ли текст; `levels` — какие уровни уверенности
    показывать оператору; `geo_enabled` — проверять ли по карте; `provider` —
    какой картой; `autofill` — писать ли адрес со степенью в карточку без
    человека; `auto_decide` (18.09) — довершать ли вердикт карты до степени
    (приблизительная точка, строка улицы без точки) или, как до 18.09,
    принимать только точный дом; `ask_*` — спрашивать ли адрес у клиента,
    когда в переписке его нет, через сколько, каким текстом и после скольких
    знаков описания. Всё, что меняет карточку или пишет клиенту само, живёт
    здесь, а не в коде: снять — одно нажатие.
    """
    расход = {
        "yandex_used_today": await _yandex_used_today(redis),
        "suggest_used_today": await _suggest_used_today(redis),
        "dadata_used_today": await _dadata_used_today(redis),
        "llm_used_today": await _llm_used_today(redis),
        "ahunter_used_today": await _ahunter_used_today(redis),
        "speller_used_today": await _speller_used_today(redis),
    }
    before = _address_detect_view(await app_settings.get_all(db), **расход)
    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch[app_settings.ADDRESS_DETECT_ENABLED] = body.enabled
    if body.autofill is not None:
        patch[app_settings.ADDRESS_DETECT_AUTOFILL] = body.autofill
    if body.levels is not None:
        patch[app_settings.ADDRESS_DETECT_LEVELS] = "".join(sorted(set(body.levels)))
    if body.geo_enabled is not None:
        patch[app_settings.ADDRESS_GEO_ENABLED] = body.geo_enabled
    if body.provider is not None:
        patch[app_settings.ADDRESS_GEO_PROVIDER] = body.provider
    if body.suggest_enabled is not None:
        patch[app_settings.ADDRESS_GEO_SUGGEST_ENABLED] = body.suggest_enabled
    if body.dadata_enabled is not None:
        patch[app_settings.ADDRESS_GEO_DADATA_ENABLED] = body.dadata_enabled
    if body.llm_enabled is not None:
        patch[app_settings.ADDRESS_LLM_ENABLED] = body.llm_enabled
    if body.ahunter_enabled is not None:
        patch[app_settings.ADDRESS_GEO_AHUNTER_ENABLED] = body.ahunter_enabled
    if body.speller_enabled is not None:
        patch[app_settings.ADDRESS_GEO_SPELLER_ENABLED] = body.speller_enabled
    if body.auto_decide is not None:
        patch[app_settings.ADDRESS_GEO_AUTO_DECIDE] = body.auto_decide
    if body.rule_policy is not None:
        # Проверка имён и значений — в реестре (`Spec.check`), одна на ручку и
        # задачу воронки; здесь только передача.
        patch[app_settings.ADDRESS_GEO_RULE_POLICY] = body.rule_policy
    if body.parse_rules is not None:
        patch[app_settings.ADDRESS_PARSE_RULES] = body.parse_rules
    if body.own_addresses is not None:
        # Проверка частей — в реестре (`Spec.check`), как у политик.
        patch[app_settings.ADDRESS_OWN_ADDRESSES] = body.own_addresses.strip()
    if body.ask_enabled:
        # Решение владельца (20.09, повторено 24.09): «сам LeadChat не должен
        # ничего спрашивать». Реплику клиенту от имени аккаунта, которую не писал
        # человек, здесь не включить ни с экрана, ни запросом.
        raise ApiError(
            "validation_error",
            "LeadChat клиентам сам не пишет — вопрос об адресе выключен насовсем",
            status=400,
            details={"fields": [{"field": "ask_enabled", "rule": "forbidden"}]},
        )
    if body.ask_enabled is False:
        patch[app_settings.ADDRESS_ASK_ENABLED] = False
    if body.ask_delay_sec is not None:
        patch[app_settings.ADDRESS_ASK_DELAY_SEC] = body.ask_delay_sec
    if body.ask_min_chars is not None:
        patch[app_settings.ADDRESS_ASK_MIN_CHARS] = body.ask_min_chars
    if body.ask_text is not None:
        текст = body.ask_text.strip()
        if not _looks_like_address_question(текст):
            raise ApiError(
                "validation_error",
                "В вопросе должно быть слово «адрес», «улица» или «куда подъехать» — "
                "иначе ответ клиента не будет прочитан как адрес",
                status=400,
                details={"fields": [{"field": "ask_text", "rule": "address_word"}]},
            )
        patch[app_settings.ADDRESS_ASK_TEXT] = текст
    if body.unlimited_yandex:
        patch[app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT] = None
    elif body.yandex_daily_limit is not None:
        patch[app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT] = body.yandex_daily_limit
    if not patch:
        return before

    after = _address_detect_view(await app_settings.set_many(db, patch, user_id=user.id), **расход)
    if after["provider"] != before["provider"]:
        # Бан — свойство провайдера, а не адреса: сменили карту — строки с
        # «карта недоступна» снова в очередь. Набор полей — общий строитель
        # (N13, 19.09): под `pending` строке карты и координатам делать нечего.
        result = await db.execute(
            sa.update(ClientAddressCandidate)
            .where(ClientAddressCandidate.geo_status == geocode.GEO_BLOCKED)
            # Снимок улик (`geo_prev`) не трогаем: у самой `blocked` улик нет
            # (ранг 0), но строка, сброшенная обходом со снимком и упёршаяся
            # в бан, снимок несёт — воркер при бане его не пишет и не стирает.
            # Стереть его здесь — следующий, возможно слепой, суд записал бы
            # вердикт слабее без удержания (ревью 20.09, C4).
            .values(
                **clients_svc.сброс_вердикта(с_попытками=False, улики=clients_svc.СНИМОК_НЕ_ТРОГАТЬ)
            )
        )
        log.info("geocode.requeue", reason="provider_changed", rows=getattr(result, "rowcount", 0))
    if after["auto_decide"] and not before["auto_decide"]:
        # Включили автопривязку — строки, судимые без неё, пересудит обход
        # починки (`address_catchup`, проверка 24.09).
        rows = await address_catchup.mark_rejudge_after_auto_decide(db, now=datetime.now(UTC))
        log.info("geocode.rejudge_marked", reason="auto_decide_on", rows=rows)
    # Строка журнала — при любом изменении И при явной подаче `rule_policy`,
    # даже если значение не изменилось: так человек останавливает объявленный
    # подъём до `exact` (задача лестницы, вето §0.3) — сохраняет ту же строку
    # «rule=approx». Задача узнаёт человека по `user_id` и `submitted`, а не
    # по разнице значений: значение `approx` до него уже вписала автоматика.
    подано = [имя for имя, значение in body.model_dump().items() if значение is not None]
    if after != before or "rule_policy" in подано:
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.address_detect_changed",
            entity="settings",
            details={"before": before, "after": after, "submitted": подано},
        )
    await db.commit()
    if after["autofill"] and not before["autofill"]:
        # Включили автозапись — адреса, подтверждённые картой, пока она была
        # выключена, ложатся в карточки сами (проверка 24.09). После commit'а:
        # воркер автозаписи читает настройку из базы.
        await enqueue_autofill_catchup(redis)
    return after


# --- подробный след пути сообщения (12 августа) -------------------------------


class TracePatch(BaseModel):
    """На сколько часов включить след. `0` — выключить сейчас."""

    hours: int = Field(default=1, ge=0, le=trace.MAX_HOURS)


def _trace_view(values: dict[str, Any]) -> dict[str, Any]:
    until = values.get(app_settings.TRACE_UNTIL)
    left = int(float(until) - time.time()) if until else 0
    return {
        "enabled": left > 0,
        # Не «включён/выключен», а СКОЛЬКО ОСТАЛОСЬ: у следа есть срок, и
        # человек, включивший его час назад, обязан видеть, что он ещё горит.
        "seconds_left": max(0, left),
        "max_hours": trace.MAX_HOURS,
    }


@router.get("/settings/trace")
async def get_trace(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return _trace_view(await app_settings.get_all(db))


@router.patch("/settings/trace")
async def patch_trace(
    body: TracePatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Включить подробный след на N часов или выключить (`hours=0`).

    ПОЧЕМУ СО СРОКОМ, А НЕ ПРОСТЫМ ПЕРЕКЛЮЧАТЕЛЕМ — разбор в `app/core/trace.py`:
    на боевом идут десятки тысяч сообщений в сутки, и след, включённый «на
    посмотреть» и забытый, надувает журналы и держит переписку клиентов в логах
    дольше нужного. По истечении срока он гаснет сам.
    """
    until = time.time() + body.hours * 3600 if body.hours else 0
    after = await app_settings.set_many(
        db, {app_settings.TRACE_UNTIL: int(until) or None}, user_id=user.id
    )
    # Свой процесс узнаёт сразу; воркер и планировщик — на ближайшем тике
    # (`trace.refresh`), то есть в пределах секунд.
    trace.set_until(float(until))
    await audit_svc.write_audit(
        db,
        user_id=user.id,
        action="settings.trace_changed",
        details={"trace_hours": body.hours},
    )
    await db.commit()
    return _trace_view(after)


# --- служебные записи Авито в ленте (14 августа) ------------------------------


class ThreadPatch(BaseModel):
    """Тело `PATCH /settings/thread`. Поле необязательно: `None` — не трогали."""

    avito_system_hidden: bool | None = None


def _thread_view(values: dict[str, Any]) -> dict[str, Any]:
    return {"avito_system_hidden": values[app_settings.AVITO_SYSTEM_HIDDEN]}


@router.get("/settings/thread")
async def get_thread_settings(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return _thread_view(await app_settings.get_all(db))


@router.patch("/settings/thread")
async def patch_thread_settings(
    body: ThreadPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Прятать ли служебные записи Авито в ленте переписки.

    Требование владельца от 14 августа, дословно: «сделай так, чтобы можно было
    отключить все системные сообщения в диалоге, которые отправляет Авито, так
    как в самой Jivo их нет».

    ⚠ РУЧКИ НЕ БЫЛО, ХОТЯ НАСТРОЙКУ УЖЕ СЛУШАЛИ. Найдено проверкой боя после
    выкатки 14 августа: лента честно спрашивает `AVITO_SYSTEM_HIDDEN` перед
    выборкой сообщений, а записать значение было НЕЧЕМ — ни эндпоинта, ни
    экрана. То есть просьба владельца была выполнена наполовину: код готов,
    выключателя нет, и человек, пришедший его искать, решил бы, что правка не
    доехала. Хуже: единственным способом переключить оставалась правка строки
    в базе руками.

    ПРЯЧЕТ, А НЕ УДАЛЯЕТ, и это главное свойство. Фильтр стоит в запросе ленты
    и только там: записи остаются в базе, приезжают в выгрузку и в «Разбор
    диалогов», а выключатель возвращает их целиком. За частью из них стоит
    действие клиента («пользователь создал чат»), и терять такое из-за
    настройки ПОКАЗА нельзя.

    НАСТРОЙКА ОБЩАЯ, А НЕ ЛИЧНАЯ, и это осознанно: диспетчеры разбирают одни и
    те же диалоги и пересказывают их друг другу. Лента, у которой на двух
    экранах разный состав, превращает «посмотри четвёртое сообщение сверху» в
    спор о том, что считать четвёртым.
    """
    before = _thread_view(await app_settings.get_all(db))

    if body.avito_system_hidden is None:
        return before

    after = _thread_view(
        await app_settings.set_many(
            db, {app_settings.AVITO_SYSTEM_HIDDEN: body.avito_system_hidden}, user_id=user.id
        )
    )
    if after != before:
        # Меняет состав ленты у всех тринадцати разом. Вопрос «куда делись
        # системные записи» обязан иметь ответ с именем и временем — иначе он
        # превращается в подозрение, что переписка теряется.
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.thread_changed",
            entity="settings",
            details={"before": before, "after": after},
        )
    await db.commit()
    return after


# --- монитор внешних сервисов (16.09) --------------------------------------------


class ApiEntryIn(BaseModel):
    """Своя запись монитора: имя обязательно, остальное — по желанию."""

    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=500)
    url: str | None = Field(default=None, max_length=500)
    docs_url: str | None = Field(default=None, max_length=500)
    daily_limit: int | None = Field(default=None, ge=0, le=100_000_000)
    monthly_limit: int | None = Field(default=None, ge=0, le=1_000_000_000)
    notes: str = Field(default="", max_length=2000)


class ApiEntryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    purpose: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    docs_url: str | None = Field(default=None, max_length=500)
    daily_limit: int | None = Field(default=None, ge=0, le=100_000_000)
    monthly_limit: int | None = Field(default=None, ge=0, le=1_000_000_000)
    #: Явное «снять потолок»: `None` в теле значит «не трогали».
    unlimited_daily: bool = False
    unlimited_monthly: bool = False
    notes: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


@router.get("/settings/apis")
async def get_apis(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Все внешние сервисы одной таблицей: ключ есть ли, включён ли, потолок,
    расход за сегодня, состояние и последняя проверка доступности."""
    return {"items": await api_monitor.overview(db, redis)}


@router.post("/settings/apis", status_code=201)
async def add_api(
    body: ApiEntryIn,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    row = await api_monitor.add_custom(
        db,
        name=body.name,
        purpose=body.purpose,
        url=body.url,
        docs_url=body.docs_url,
        daily_limit=body.daily_limit,
        monthly_limit=body.monthly_limit,
        notes=body.notes,
        user_id=user.id,
    )
    await audit_svc.write_audit(
        db,
        user_id=user.id,
        action="settings.api_added",
        entity="api_registry",
        entity_id=row.key,
        details={"name": row.name, "url": row.url, "daily_limit": row.daily_limit},
    )
    await db.commit()
    return {"key": row.key, "items": await api_monitor.overview(db, redis)}


async def _своя_запись(db: AsyncSession, key: str) -> ApiRegistryEntry:
    row = await db.scalar(sa.select(ApiRegistryEntry).where(ApiRegistryEntry.key == key))
    if row is None:
        raise HTTPException(status_code=404, detail="Такой записи нет")
    return row


@router.patch("/settings/apis/{key}")
async def patch_api(
    key: str,
    body: ApiEntryPatch,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    row = await _своя_запись(db, key)

    def снимок() -> dict[str, Any]:
        return {
            "name": row.name,
            "purpose": row.purpose,
            "url": row.url,
            "docs_url": row.docs_url,
            "daily_limit": row.daily_limit,
            "monthly_limit": row.monthly_limit,
            "notes": row.notes,
            "enabled": row.enabled,
        }

    было = снимок()
    if body.name is not None:
        row.name = body.name.strip()
    if body.purpose is not None:
        row.purpose = body.purpose.strip()
    if body.url is not None:
        row.url = body.url.strip() or None
    if body.docs_url is not None:
        row.docs_url = body.docs_url.strip() or None
    if body.unlimited_daily:
        row.daily_limit = None
    elif body.daily_limit is not None:
        row.daily_limit = body.daily_limit
    if body.unlimited_monthly:
        row.monthly_limit = None
    elif body.monthly_limit is not None:
        row.monthly_limit = body.monthly_limit
    if body.notes is not None:
        row.notes = body.notes.strip()
    if body.enabled is not None:
        row.enabled = body.enabled
    стало = снимок()
    if стало != было:
        await audit_svc.write_audit(
            db,
            user_id=user.id,
            action="settings.api_changed",
            entity="api_registry",
            entity_id=row.key,
            details={"before": было, "after": стало},
        )
    await db.commit()
    return {"items": await api_monitor.overview(db, redis)}


@router.delete("/settings/apis/{key}")
async def delete_api(
    key: str,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    row = await _своя_запись(db, key)
    await audit_svc.write_audit(
        db,
        user_id=user.id,
        action="settings.api_removed",
        entity="api_registry",
        entity_id=row.key,
        details={"name": row.name},
    )
    await db.delete(row)
    await db.commit()
    return {"items": await api_monitor.overview(db, redis)}


@router.post("/settings/apis/{key}/check")
async def check_api(
    key: str,
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Сходить по адресу сервиса с сервера и запомнить итог на сутки.

    По нажатию, не по расписанию: это поход наружу с боевого сервера.
    Транзакция чтения закрывается до похода: шесть секунд ожидания ответа —
    не повод держать соединение из пула «idle in transaction» (ревью 16.09).
    """
    spec = await api_monitor.probe_spec(db, redis, key)
    if spec is None:
        raise HTTPException(status_code=404, detail="Такого сервиса нет")
    await db.rollback()
    итог = await api_monitor.probe(redis, key, spec)
    return {"key": key, "checked": итог, "items": await api_monitor.overview(db, redis)}
