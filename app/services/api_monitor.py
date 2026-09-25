"""Монитор внешних сервисов: кто подключён, какой у кого потолок, сколько
выбрано сегодня и жив ли сервис (владелец 16.09: «монитор по API, где
можно вручную обновлять, добавлять API и смотреть лимиты»).

ДВА РОДА ЗАПИСЕЙ. Встроенные — те, что зовёт код: их ключи лежат на шлюзе
Амстердама (снимок `/status` говорит, у кого ключ есть), переключатели — в
настройках, расход — в счётчиках Redis воркера, а простои — в его же флагах
(`geo:blocked:*`, `geo:exhausted:*`, `geo:ahunter:down`). Их состояние здесь
СЧИТАЕТСЯ, а не хранится. Свои —
записи владельца в `api_registry`: имя, адрес, лимиты, заметка; расход у
них не считается (код их не зовёт), а состояние даёт только проверка
доступности.

ТРЕТИЙ РОД — СТРОКА ВОРОНКИ АДРЕСОВ (18.09). Это не сервис, а замер того,
что сервисы выше делают вместе: доля диалогов с адресом в переписке, у
которых адрес лёг в карточку, и степень точки. Считается раз в неделю
планировщиком (`scheduler/jobs/address_funnel`), здесь только читается:
последняя неделя рядом с прошлой; падение доли — красная строка, та же
причина, что в уведомлении `address.funnel_dropped`. Адреса пробы у неё нет
— «Проверить» её пропускает.

ПРОВЕРКА ДОСТУПНОСТИ — по нажатию, не по расписанию: это поход наружу, и
делать его каждую минуту ко всем сервисам незачем; итог живёт в Redis сутки
и показывается с временем. С 16.09 ключи и сеть провайдеров — на шлюзе
Амстердама (docs/46), поэтому и пробы делает он: встроенные — `POST
/check/{provider}` тем же адресом и ключом, которыми ходит работа; свои
записи — `POST /check/url`, где сторож «не внутрь» и правило «по редиректам
не идём» живут вместе с сетью. Единственный прямой поход монитора — `GET
/health` самого шлюза: строка «Шлюз API (Амстердам)» идёт первой, без неё
всё остальное «нет связи со шлюзом».
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations import gateway
from app.models.api_registry import ApiRegistryEntry
from app.services import address_funnel, app_settings

log = structlog.get_logger()

#: Проба на шлюзе ждёт провайдера до шести секунд; поход в шлюз — на две
#: секунды дольше, чтобы ответ «провайдер не ответил» дошёл словами, а не
#: превратился в обрыв на нашей стороне.
PROBE_TIMEOUT_SEC = 6.0
PROBE_TIMEOUT = httpx.Timeout(PROBE_TIMEOUT_SEC, connect=3.0)
PROBE_TTL_SEC = 24 * 3600
USER_AGENT = "LeadChat/1.0 (api-monitor)"
#: Ключ строки самого шлюза — первой в мониторе; её проба идёт мимо шлюза.
GATEWAY_KEY = "gateway"
#: Ключ строки воронки адресов — последней в мониторе; пробы у неё нет.
FUNNEL_KEY = "address_funnel"

#: Состояния строки — что увидит человек в одной колонке.
STATE_OK = "ok"  # подключён, в потолке
STATE_OFF = "off"  # выключен переключателем
STATE_NO_KEY = "no_key"  # нужен ключ, а его нет
STATE_LIMIT = "limit"  # суточный потолок выбран или сервис закрыл на сутки
STATE_DOWN = "down"  # лежит: бан/кулдаун от воркера или проверка не прошла
STATE_UNKNOWN = "unknown"  # свой сервис без проверки


@dataclass(frozen=True, slots=True)
class ApiRow:
    key: str
    kind: str  # builtin | custom
    name: str
    purpose: str
    url: str | None
    docs_url: str | None
    env_var: str | None
    key_present: bool | None  # None — ключ не нужен
    enabled: bool
    daily_limit: int | None
    monthly_limit: int | None
    used_today: int | None  # None — не считается
    state: str
    state_note: str
    notes: str
    checked: dict[str, Any] | None  # последняя проверка доступности
    editable: bool


@dataclass(frozen=True, slots=True)
class Probe:
    """Чем проверять сервис. `provider` — встроенный: шлюз проверяет его сам,
    своим адресом и ключом (`POST /check/{provider}`); адрес, заголовки и
    метод пробы живут там. `url` — своя запись владельца (`POST /check/url`
    со сторожем на шлюзе) или строка самого шлюза (`GET /health` напрямую).
    """

    provider: str | None = None
    url: str = ""


def _шлюз() -> str:
    return settings.gateway_url.strip().rstrip("/")


async def overview(db: AsyncSession, redis: Redis) -> list[dict[str, Any]]:
    # Снимок ключей — ДО первого чтения из базы: поход к шлюзу (до 8 с при
    # лежащем мосте) не должен держать открытую транзакцию запроса.
    await gateway.refresh_status()
    values = await app_settings.get_all(db)
    rows, _ = await _встроенные(values, redis)
    return [asdict(r) for r in [*rows, *await _свои(db, redis), await funnel_row(db)]]


async def probe_spec(db: AsyncSession, redis: Redis, key: str) -> Probe | None:
    """Чем проверять сервис по ключу: встроенный — именем провайдера для шлюза
    (или адресом `/health` для строки самого шлюза), свой — адресом из записи;
    неизвестный — None."""
    await gateway.refresh_status()
    values = await app_settings.get_all(db)
    _, пробы = await _встроенные(values, redis)
    if key in пробы:
        return пробы[key]
    row = await db.scalar(sa.select(ApiRegistryEntry).where(ApiRegistryEntry.key == key))
    if row is None:
        return None
    return Probe(url=row.url or "")


# --- встроенные -----------------------------------------------------------------


async def _встроенные(
    values: dict[str, Any], redis: Redis
) -> tuple[list[ApiRow], dict[str, Probe]]:
    from app.workers import geocode as worker
    from app.workers.address_llm import llm_calls_today

    гео = bool(values[app_settings.ADDRESS_GEO_ENABLED])
    провайдер = str(values[app_settings.ADDRESS_GEO_PROVIDER] or "nominatim")
    llm_вкл = bool(values[app_settings.ADDRESS_LLM_ENABLED])
    llm_потолок = values[app_settings.ADDRESS_LLM_DAILY_LIMIT]
    llm_расход = await llm_calls_today(redis)
    до_полуночи = f"до полуночи по Москве ({_осталось_до_полуночи()})"
    # Есть ли ключи — знает только шлюз: снимок `/status` (раз в минуту,
    # обновляют `overview`/`probe_spec` до открытия транзакции). Снимка нет
    # (шлюз не ответил ни разу) — ключи считаем заданными и говорим об этом
    # в строке шлюза: «нет ключа» и «нет связи» — разные состояния, и
    # подменять одно другим нельзя. Снимок есть, а последний поход за ним
    # не удался — шлюз лёг после: строка шлюза краснеет с причиной.
    снимок = gateway.known_keys
    сбой_снимка = gateway.last_status_error
    шлюз = _шлюз()

    def ключ_на_шлюзе(provider: str) -> bool:
        return True if снимок is None else bool(снимок.get(provider))

    async def бан(provider: str) -> bool:
        return await worker._провайдер_забанен(redis, provider)

    async def закрыт(provider: str) -> bool:
        return await worker._закрыт_на_сутки(redis, provider)

    rows: list[ApiRow] = []
    пробы: dict[str, Probe] = {}

    async def строка(
        *,
        key: str,
        name: str,
        purpose: str,
        probe: Probe,
        url: str | None,
        docs_url: str | None,
        env_var: str | None,
        key_present: bool | None,
        enabled: bool,
        daily_limit: int | None,
        used_today: int | None,
        down: bool = False,
        down_note: str = "",
        limit_closed: bool = False,
        off_note: str = "выключен в настройках",
        note: str = "",
        no_key_note: str | None = None,
    ) -> None:
        checked = await _последняя_проверка(redis, key)
        if not enabled:
            state, why = STATE_OFF, off_note
        elif env_var and not key_present:
            state, why = STATE_NO_KEY, no_key_note or f"ключ {env_var} на сервере не задан"
        elif down:
            state, why = STATE_DOWN, down_note or "сервис отказывает — воркер его не спрашивает"
        elif limit_closed or (
            daily_limit is not None and used_today is not None and used_today >= daily_limit
        ):
            state, why = STATE_LIMIT, f"суточный потолок выбран — не спрашивается {до_полуночи}"
        elif checked is not None and not checked.get("ok"):
            ошибка = checked.get("error") or checked.get("status")
            state, why = STATE_DOWN, f"проверка не прошла: {ошибка}"
        else:
            state, why = STATE_OK, ""
        пробы[key] = probe
        rows.append(
            ApiRow(
                key=key,
                kind="builtin",
                name=name,
                purpose=purpose,
                url=url,
                docs_url=docs_url,
                env_var=env_var,
                key_present=key_present,
                enabled=enabled,
                daily_limit=daily_limit,
                monthly_limit=None,
                used_today=used_today,
                state=state,
                state_note=why,
                notes=note,
                checked=checked,
                editable=False,
            )
        )

    async def встроенный(
        *,
        key: str,
        name: str,
        purpose: str,
        docs_url: str,
        env_var: str | None,
        enabled: bool,
        daily_limit: int | None,
        used_today: int | None,
        down: bool = False,
        down_note: str = "",
        limit_closed: bool = False,
        off_note: str = "выключен в настройках",
        note: str = "",
    ) -> None:
        """Провайдер за шлюзом: ключ — по снимку, проба — `/check/{key}`.
        Экрану — адрес пробы: кнопке «Проверить» нужен непустой адрес."""
        await строка(
            key=key,
            name=name,
            purpose=purpose,
            probe=Probe(provider=key),
            url=f"{шлюз}/check/{key}",
            docs_url=docs_url,
            env_var=env_var,
            key_present=ключ_на_шлюзе(key) if env_var else None,
            enabled=enabled,
            daily_limit=daily_limit,
            used_today=used_today,
            down=down,
            down_note=down_note,
            limit_closed=limit_closed,
            off_note=off_note,
            note=note,
            # Ключи помощников с 18.09 лежат на шлюзе (docs/46), а не в .env
            # LeadChat: правка своего .env ничего бы не дала (проверка 24.09).
            no_key_note=f"у шлюза внешних API нет ключа {env_var}",
        )

    # Строка самого шлюза — первой: без него всё ниже не работает. «Включён»
    # = адрес задан; ключ = токен; «не отвечает» = снимка ключей нет.
    await строка(
        key=GATEWAY_KEY,
        name="Шлюз API (Амстердам)",
        purpose="Ключи и походы ко всем помощникам ниже; лёг — они молчат",
        probe=Probe(url=f"{шлюз}/health" if шлюз else ""),
        url=f"{шлюз}/health" if шлюз else None,
        docs_url=None,
        env_var="GATEWAY_TOKEN",
        key_present=bool(settings.gateway_token.strip()),
        enabled=bool(шлюз),
        daily_limit=None,
        used_today=None,
        down=снимок is None or сбой_снимка is not None,
        down_note=(
            "снимок ключей не получен — шлюз не отвечает"
            if снимок is None
            else f"шлюз перестал отвечать: {сбой_снимка}"
        ),
        off_note="адрес шлюза GATEWAY_URL не задан",
        note="слушает только внутри WireGuard; без базы и потолков",
    )
    выкл_карты = "проверка по карте выключена" if not гео else "выключен в настройках"
    await встроенный(
        key="dadata",
        name="DaData «Подсказки»",
        purpose="Адрес по ФИАС с координатами — первый в цепочке проверки",
        docs_url="https://dadata.ru/api/suggest/address/",
        env_var="DADATA_API_KEY",
        enabled=гео and bool(values[app_settings.ADDRESS_GEO_DADATA_ENABLED]),
        daily_limit=values[app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT],
        used_today=await worker.dadata_calls_today(redis),
        down=await бан("dadata"),
        limit_closed=await закрыт("dadata"),
        off_note=выкл_карты,
        note="бесплатно 10 000 запросов в сутки на все методы",
    )
    await встроенный(
        key="nominatim",
        name="OpenStreetMap (Nominatim)",
        purpose="Карта без ключа: дом по улице и городу",
        docs_url="https://operations.osmfoundation.org/policies/nominatim/",
        env_var=None,
        enabled=гео and провайдер in ("nominatim", "osm_then_yandex"),
        daily_limit=None,
        used_today=None,
        down=await бан("nominatim"),
        down_note="три блокировки за час — воркер его не спрашивает",
        off_note=выкл_карты if not гео else "картой выбран Яндекс",
        note="политика: не чаще 1 запроса в секунду, без потолка",
    )
    await встроенный(
        key="yandex_geocoder",
        name="Яндекс Геокодер",
        purpose="Карта с лучшим покрытием России — после OSM или вместо него",
        docs_url="https://yandex.ru/maps-api/docs/geocoder-api/",
        env_var="YANDEX_GEOCODER_KEY",
        enabled=гео and провайдер in ("yandex", "osm_then_yandex"),
        daily_limit=values[app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT],
        used_today=await worker.yandex_calls_today(redis),
        down=await бан("yandex"),
        limit_closed=await закрыт("yandex"),
        off_note=выкл_карты if not гео else "картой выбран только OpenStreetMap",
        note="бесплатно 1 000 в сутки; лицензия — для открытых сайтов",
    )
    await встроенный(
        key="yandex_suggest",
        name="Яндекс Геосаджест",
        purpose="Подсказка при опечатке или сокращении; организации по названию",
        docs_url="https://yandex.ru/maps-api/docs/suggest-api/",
        env_var="YANDEX_SUGGEST_KEY",
        enabled=гео and bool(values[app_settings.ADDRESS_GEO_SUGGEST_ENABLED]),
        daily_limit=values[app_settings.ADDRESS_GEO_SUGGEST_DAILY_LIMIT],
        used_today=await worker.suggest_calls_today(redis),
        down=await бан("yandex_suggest"),
        limit_closed=await закрыт("yandex_suggest"),
        off_note=выкл_карты,
        note="бесплатно 1 000 в сутки",
    )
    await встроенный(
        key="ahunter",
        name="Ahunter (ГАР)",
        purpose="Второй справочник адресов без ключа: кварталы, корпуса, СНТ",
        docs_url="https://ahunter.ru/site/suggest",
        env_var=None,
        enabled=гео and bool(values[app_settings.ADDRESS_GEO_AHUNTER_ENABLED]),
        daily_limit=None,
        used_today=await worker.ahunter_calls_today(redis),
        down=await worker._ahunter_лежит(redis),
        down_note="не ответил воркеру — пауза десять минут",
        off_note=выкл_карты,
        note="без ключа и без потолка; один сервер, без SLA",
    )
    await встроенный(
        key="speller",
        name="Яндекс Спеллер",
        purpose="Опечатки в названии улицы до похода к карте",
        docs_url="https://yandex.ru/dev/speller/",
        env_var=None,
        enabled=гео and bool(values[app_settings.ADDRESS_GEO_SPELLER_ENABLED]),
        daily_limit=values[app_settings.ADDRESS_GEO_SPELLER_DAILY_LIMIT],
        used_today=await worker.speller_calls_today(redis),
        off_note=выкл_карты,
        note="бесплатно 10 000 запросов и 10 млн символов в сутки",
    )
    for key, name, env_var, docs, note in (
        (
            "openrouter",
            "OpenRouter (бесплатные модели)",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/docs/api-reference/limits",
            "50 в сутки без кредитов, 1 000 при купленных от $10",
        ),
        (
            "groq",
            "Groq (запасной читатель)",
            "GROQ_API_KEY",
            "https://console.groq.com/settings/limits",
            "бесплатно 1 000 запросов в сутки на модель",
        ),
        (
            "mistral",
            "Mistral (запасной читатель)",
            "MISTRAL_API_KEY",
            "https://admin.mistral.ai/subscription",
            "план Free — $10 кредитов в месяц",
        ),
    ):
        # Потолок и расход — общие на всех читателей; у читателя без ключа
        # их не показываем: он в цепочке не участвует.
        с_ключом = ключ_на_шлюзе(key)
        await встроенный(
            key=key,
            name=name,
            purpose="Модель перечитывает адрес, когда правила промолчали или карта отказала",
            docs_url=docs,
            env_var=env_var,
            enabled=llm_вкл,
            daily_limit=llm_потолок if с_ключом else None,
            used_today=llm_расход if с_ключом else None,
            note=note + " · потолок и расход общие на всех читателей",
        )
    await встроенный(
        key="anthropic",
        name="Anthropic (Claude, боты)",
        purpose="Ответы ботов и классификация — только для ботов, не для адресов",
        docs_url="https://docs.anthropic.com/",
        env_var="ANTHROPIC_API_KEY",
        enabled=True,
        daily_limit=None,
        used_today=None,
        note="платный, расход здесь не считается; зовётся только воркером ботов",
    )
    return rows, пробы


def _осталось_до_полуночи() -> str:
    from zoneinfo import ZoneInfo

    сейчас = datetime.now(ZoneInfo("Europe/Moscow"))
    минут = (24 * 60) - (сейчас.hour * 60 + сейчас.minute)
    return f"через {минут // 60} ч {минут % 60:02d} мин"


# --- свои -------------------------------------------------------------------------


async def _свои(db: AsyncSession, redis: Redis) -> list[ApiRow]:
    rows = (
        await db.execute(sa.select(ApiRegistryEntry).order_by(ApiRegistryEntry.created_at))
    ).scalars()
    out: list[ApiRow] = []
    for r in rows:
        checked = await _последняя_проверка(redis, r.key)
        if not r.enabled:
            state, why = STATE_OFF, "выключен"
        elif checked is None:
            state, why = STATE_UNKNOWN, "проверка ещё не запускалась"
        elif checked.get("ok"):
            state, why = STATE_OK, f"проверка: ответ {checked.get('status')}"
        else:
            state, why = STATE_DOWN, str(checked.get("error") or checked.get("status") or "")
        out.append(
            ApiRow(
                key=r.key,
                kind="custom",
                name=r.name,
                purpose=r.purpose,
                url=r.url,
                docs_url=r.docs_url,
                env_var=None,
                key_present=None,
                enabled=r.enabled,
                daily_limit=r.daily_limit,
                monthly_limit=r.monthly_limit,
                used_today=None,
                state=state,
                state_note=why,
                notes=r.notes,
                checked=checked,
                editable=True,
            )
        )
    return out


# --- воронка адресов ------------------------------------------------------------


async def funnel_row(db: AsyncSession) -> ApiRow:
    """Строка «адреса в карточку» — последняя неделя рядом с прошлой.

    Состояние: снимка нет — «не проверялся» (первый замер в понедельник);
    ровно — «работает» с долями в подписи; `compare` дал причины — «лежит»
    с теми же словами, что в уведомлении. Таблица недоступна (миграция
    0079 не прошла) — «лежит» с именем ошибки, а не пятисотка на весь
    монитор: остальные строки человеку нужнее.
    """
    try:
        снимок = await address_funnel.latest(db)
    except sa.exc.SQLAlchemyError as exc:
        log.warning("api_monitor.funnel_unavailable", error=type(exc).__name__)
        return _строка_воронки(
            state=STATE_DOWN,
            why=f"снимок недели не читается: {type(exc).__name__}",
            note="",
        )
    if снимок is None:
        return _строка_воронки(
            state=STATE_UNKNOWN,
            why="первый замер — в понедельник 04:40 МСК (или address-funnel --store)",
            note="",
        )
    c = снимок.counts
    причины = address_funnel.compare(снимок.prev, c)
    доли = (
        f"неделя с {снимок.week_start:%d.%m}: диалогов с адресом {c.dialogs}, "
        f"строка разбора у {c.with_row} ({address_funnel.share_pct(c.with_row, c.dialogs)} %), "
        f"в карточке {c.card} ({address_funnel.share_pct(c.card, c.dialogs)} %)"
    )
    if снимок.prev is not None:
        доли += (
            f"; неделей раньше в карточке "
            f"{address_funnel.share_pct(снимок.prev.card, снимок.prev.dialogs)} %"
        )
    why = f"падение: {address_funnel.reason_words(причины)} · {доли}" if причины else доли
    note = (
        f"точных {c.card_auto_exact} · приблизительных {c.card_auto_approx} · "
        f"без точки {c.card_auto_text} · руками или кнопкой {c.card_person} · "
        f"без источника {c.card_unknown} · правок поверх авто {c.edited_after_auto} · "
        f"вопросов задано {c.asked}"
    )
    return _строка_воронки(state=STATE_DOWN if причины else STATE_OK, why=why, note=note)


def _строка_воронки(*, state: str, why: str, note: str) -> ApiRow:
    return ApiRow(
        key=FUNNEL_KEY,
        kind="builtin",
        name="Адреса в карточку (воронка за неделю)",
        purpose="Сколько адресов из переписки дошло до карточки и с какой степенью",
        url=None,
        docs_url=None,
        env_var=None,
        key_present=None,
        enabled=True,
        daily_limit=None,
        monthly_limit=None,
        used_today=None,
        state=state,
        state_note=why,
        notes=note,
        checked=None,
        editable=False,
    )


#: Ключи встроенных строк — своя запись их не занимает: иначе две строки с
#: одним ключом делят слот проверки, а PATCH/DELETE находят «свою» под именем
#: встроенной (ревью 16.09).
BUILTIN_KEYS = frozenset(
    {
        "dadata",
        "nominatim",
        "yandex_geocoder",
        "yandex_suggest",
        "ahunter",
        "speller",
        "openrouter",
        "groq",
        "mistral",
        "anthropic",
        GATEWAY_KEY,
        FUNNEL_KEY,
    }
)
_КЛЮЧ = re.compile(r"[^a-z0-9]+")


def make_key(name: str) -> str:
    """«2ГИС demo» → «2gis-demo»: латиница и цифры; кириллица транслитерируется."""
    таблица = str.maketrans(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "abvgdeejzijklmnoprstufhccss_y_eua",
    )
    сырой = name.strip().lower().translate(таблица)
    ключ = _КЛЮЧ.sub("-", сырой).strip("-")
    return ключ[:40] or "api"


async def add_custom(
    db: AsyncSession,
    *,
    name: str,
    purpose: str,
    url: str | None,
    docs_url: str | None,
    daily_limit: int | None,
    monthly_limit: int | None,
    notes: str,
    user_id: uuid.UUID | None,
) -> ApiRegistryEntry:
    основа = make_key(name)
    ключ = основа
    n = 2
    while ключ in BUILTIN_KEYS or await db.scalar(
        sa.select(ApiRegistryEntry.id).where(ApiRegistryEntry.key == ключ)
    ):
        ключ = f"{основа}-{n}"
        n += 1
    row = ApiRegistryEntry(
        key=ключ,
        name=name.strip(),
        purpose=purpose.strip(),
        url=(url or "").strip() or None,
        docs_url=(docs_url or "").strip() or None,
        daily_limit=daily_limit,
        monthly_limit=monthly_limit,
        notes=notes.strip(),
        created_by_id=user_id,
    )
    db.add(row)
    await db.flush()
    return row


# --- проверка доступности -----------------------------------------------------------


async def probe(
    redis: Redis,
    key: str,
    spec: Probe | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Проверить сервис и запомнить итог на сутки.

    Встроенный — `POST /check/{provider}` на шлюзе: у него адрес, ключ и
    сеть; своя запись — `POST /check/url`, сторож «не внутрь» и запрет
    редиректов там же. Строка самого шлюза — прямой `GET /health`, единственный
    поход монитора мимо шлюза. Шлюз не ответил — итог «нет связи со шлюзом»,
    а не «сервис лежит»: это разные беды. `client` — для проверок.
    """
    итог: dict[str, Any]
    if key == GATEWAY_KEY and spec is not None and spec.url:
        итог = await _проба_шлюза(spec.url, client=client)
    elif spec is not None and spec.provider:
        итог = await _через_шлюз(f"/check/{spec.provider}", None, client=client)
    elif spec is not None and spec.url:
        итог = await _через_шлюз("/check/url", {"url": spec.url}, client=client)
    else:
        итог = {"ok": False, "status": None, "ms": None, "error": "адрес не задан"}
    итог["at"] = datetime.now(UTC).isoformat()
    try:
        await redis.set(f"api:check:{key}", json.dumps(итог), ex=PROBE_TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — итог показан, не сохранён
        log.warning("api_monitor.check_not_saved", key=key, error=type(exc).__name__)
    return итог


async def _через_шлюз(
    path: str, payload: dict[str, Any] | None, *, client: httpx.AsyncClient | None
) -> dict[str, Any]:
    try:
        ответ = await gateway.call(path, payload, timeout=PROBE_TIMEOUT_SEC + 2, client=client)
    except gateway.GatewayError as exc:
        # Словами для администратора: род беды и, чем богаты, — код ответа
        # шлюза или имя ошибки httpx (без адреса: он в тексте исключения).
        уточнение = exc.status or exc.detail
        почему = f"нет связи со шлюзом: {exc.kind}" + (f" ({уточнение})" if уточнение else "")
        return {"ok": False, "status": None, "ms": None, "error": почему}
    итог = ответ.get("result")
    if not isinstance(итог, dict) or "ok" not in итог:
        log.warning("api_monitor.check_bad_shape", path=path)
        return {"ok": False, "status": None, "ms": None, "error": "ответ шлюза не той формы"}
    return {
        "ok": bool(итог.get("ok")),
        "status": итог.get("status"),
        "ms": итог.get("ms"),
        "error": итог.get("error"),
    }


async def _проба_шлюза(url: str, *, client: httpx.AsyncClient | None) -> dict[str, Any]:
    """`GET /health` шлюза без токена: жив — 200 и `{"ok": true}`."""
    headers = {"User-Agent": USER_AGENT}
    начало = time.monotonic()
    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT, headers=headers, follow_redirects=False
            ) as own:
                resp = await own.get(url)
        else:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        мс = round((time.monotonic() - начало) * 1000)
        # Тип исключения — да, текст — нет: в тексте httpx есть URL.
        log.warning("api_monitor.gateway_unreachable", error=type(exc).__name__)
        return {
            "ok": False,
            "status": None,
            "ms": мс,
            "error": f"нет связи со шлюзом ({type(exc).__name__})",
        }
    мс = round((time.monotonic() - начало) * 1000)
    try:
        жив = resp.status_code == 200 and resp.json().get("ok") is True
    except (ValueError, AttributeError):
        жив = False
    if жив:
        return {"ok": True, "status": resp.status_code, "ms": мс, "error": None}
    return {
        "ok": False,
        "status": resp.status_code,
        "ms": мс,
        "error": f"нет связи со шлюзом: ответ {resp.status_code}",
    }


async def _последняя_проверка(redis: Redis, key: str) -> dict[str, Any] | None:
    try:
        сырой = await redis.get(f"api:check:{key}")
    except Exception as exc:  # noqa: BLE001
        log.warning("api_monitor.check_read_failed", key=key, error=type(exc).__name__)
        return None
    if not сырой:
        return None
    try:
        данные = json.loads(сырой)
    except ValueError:
        return None
    return данные if isinstance(данные, dict) else None
