"""Подключение аккаунтов Авито: OAuth-флоу и управление (01 §4, DESIGN §8.1).

- `GET /avito/connect-url` — JSON с URL страницы согласия (редирект делает
  фронт: браузерный переход не несёт Authorization — 01 §4.2);
- `GET|POST /avito/callback` — публичный: state одноразовый (Redis GETDEL,
  TTL 600), обмен кода, get_self, upsert аккаунта с шифрованными токенами,
  регистрация вебхука, постановка backfill; ответ — 302 на страницу настроек;
- `POST /avito-accounts/{id}/reconnect` — тот же флоу для needs_reauth,
  state помечен reconnect: callback обновляет токены существующей записи
  (матчинг по avito_user_id, чужой аккаунт -> ?error=account_mismatch);
- `GET /avito-accounts` — список без токенов и webhook_secret (никогда);
- `POST /avito-accounts/{id}/disable|enable`;
- `GET /avito-accounts/{id}/history-size` — сколько диалогов и сообщений
  сотрёт любое из двух необратимых действий; спрашивается перед показом
  подтверждения, в списке карточек не считается (дорого и почти всегда зря).

Назначение операторов на канал (план 7.2) живёт в соседнем модуле
`app/api/routes/avito_accounts.py`; сюда из него приходит только свод
`operators` — строка «Операторы: N» и аватарки на карточке (11 §4.1).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, NoReturn

import httpx
import structlog
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.core.config import settings
from app.core.errors import ApiError
from app.integrations.avito.client import AvitoClient, build_authorize_url
from app.integrations.avito.errors import AvitoApiError, AvitoAuthError, AvitoUnavailable
from app.models import AvitoAccount, Conversation, Message, User
from app.models.lead import SRC_KEYS
from app.schemas.account_operators import ChannelOperatorsSummary
from app.services import account_operators as operators_service
from app.services import account_stats, avito_app, channel_health
from app.services import avito_accounts as accounts_service
from app.services.audit import write_audit

router = APIRouter()
log = structlog.get_logger("app.avito_connect")

# TTL одноразового state — в сервисе (accounts_service.OAUTH_STATE_TTL_SECONDS):
# его же использует кнопка «Переподключить» центра уведомлений.
SETTINGS_ACCOUNTS_PATH = "/settings/accounts"


# --- схемы ответов (без токенов и webhook_secret — 01 §4.1) -----------------


class ConnectUrlOut(BaseModel):
    url: str


class WebhookOut(BaseModel):
    """Строка «Webhook …» карточки канала.

    ПОЛЯ РАЗДЕЛЕНЫ НА ДВА СОРТА, И РАЗНИЦА МЕЖДУ НИМИ — ГЛАВНОЕ В ЭТОЙ СХЕМЕ.

    ``status`` и ``last_event_at`` — сырьё: «в тот раз Авито подписку принял» и
    «когда к нам последний раз что-то пришло». Из этих двух чисел экран и
    делал вывод сам, и вывод получался неверный: на канале «Дамир» (пять
    обращений в неделю) вечно горело «⚠️ событий нет 16 ч» с текстом про
    подписку, которая могла достаться другой системе, — при полностью
    исправной интеграции.

    ``state`` и остальное — ВЫВОД, сделанный на сервере
    (``app/services/channel_health.py``): тишина сравнена с обычным ритмом
    этого канала в рабочие часы, а подписка перед обвинением ПРОВЕРЕНА у
    Авито. Экран рисует ``state`` и ``message``, а не считает заново; иначе
    правило «когда пугать человека» существовало бы в двух местах и разъехалось
    бы на первой же правке.
    """

    #: Память о последней попытке подписаться: ok | failed | not_registered |
    #: unregister_failed. Оставлено как было — на него смотрит и код, и экран.
    status: str
    url: str | None = None
    last_event_at: str | None = None

    # --- вывод сервера (12 августа) -----------------------------------------
    #: ok — события идут; quiet — событий нет, и для этого канала это норма
    #: (НЕЙТРАЛЬНО, без знака тревоги); warning — жёлтое; critical — красное.
    state: str = channel_health.OK
    #: Машинный повод: subscription_foreign | subscription_none |
    #: register_failed | not_registered | unregister_failed | silence_abnormal |
    #: quiet_but_normal | quiet_unknown_rhythm | no_events_yet | disabled |
    #: service_stub | null.
    reason: str | None = None
    #: Готовая строка по-русски — ровно то, что показывать человеку.
    message: str = ""
    #: ⚠ ДВЕ ЧАСТИ ТОЙ ЖЕ ФРАЗЫ (разбор интерфейса 13.08). `headline` — что случилось
    #: и чем грозит, одной фразой; `detail` — числа, ритм канала, время сверки,
    #: причина отказа. Экран рисует первую янтарной со значком, вторую мельче и серым.
    #:
    #: `message` при этом НЕ УКОРОЧЕН и остаётся полем-страховкой: сервер и фронт
    #: выкатываются порознь, и старая сборка обязана показать фразу целиком. Новая
    #: при старом сервере увидит пустой `headline` и нарисует `message`.
    headline: str = ""
    detail: str = ""
    #: Тишина в минутах: астрономическая и в рабочих часах (окно берётся из
    #: настроек «Распределение»). Ночь рабочей тишиной не считается.
    silence_minutes: int | None = None
    quiet_minutes: int | None = None
    #: Обычная рабочая пауза канала (медиана за ``rhythm_window_days``) и
    #: сколько пауз измерено. ``null`` — ритма не знаем, и тогда тревога по
    #: тишине не поднимается вовсе.
    rhythm_minutes: int | None = None
    rhythm_samples: int = 0
    rhythm_window_days: int = channel_health.RHYTHM_WINDOW_DAYS
    #: С чем сравнивается ``quiet_minutes``.
    threshold_minutes: int | None = None
    #: Когда последний раз сверяли подписку с Авито и что она показала:
    #: ours | foreign | none | unknown | null (не сверяли ни разу).
    checked_at: datetime | None = None
    check_result: str | None = None
    #: Какую кнопку показывать: rewebhook | null.
    action: str | None = None


class TokenOut(BaseModel):
    """Строка «Токен …» карточки канала.

    ЗАЧЕМ ОНА ПОЯВИЛАСЬ. Наружу ехало одно поле — ``token_expires_at``, — и
    экран сам решал, пугать ли им человека. Решал так: меньше суток до
    истечения → жёлтая строка со знаком «⚠️». Токен Авито живёт сутки, значит
    жёлтая строка горела на всех каналах круглосуточно, и люди жали «Обновить
    токен» руками каждый день, хотя перевыпуск идёт сам.

    Теперь состояний три и считает их сервер — вместе с тем, чего экрану
    неоткуда узнать: получалось ли автообновление и когда в последний раз.
    """

    #: ok — нейтрально, без значка; warning — жёлтое; critical — красное.
    state: str
    #: expired | refresh_broken | refresh_failed | expiring | disabled |
    #: service_stub | null.
    reason: str | None = None
    message: str = ""
    #: Те же две части — разбор записан у WebhookOut выше.
    headline: str = ""
    detail: str = ""
    expires_at: datetime | None = None
    #: Сколько минут осталось; у истёкшего — 0, а не отрицательное число.
    expires_in_minutes: int = 0
    #: Когда планировщик возьмётся за токен сам (срок минус два часа).
    auto_refresh_at: datetime | None = None
    #: Когда обновление ПОСЛЕДНИЙ РАЗ получилось. ``null`` — на нашей памяти
    #: ни разу: канал только подключили или Redis перезапускали.
    last_refresh_at: datetime | None = None
    #: Чем кончилась последняя попытка; ``null`` — попыток не было.
    last_refresh_ok: bool | None = None
    #: Причина последней неудачи, по-русски и коротко.
    last_error: str | None = None
    #: Неудач подряд.
    failures: int = 0
    #: Какую кнопку показывать: refresh_token | null.
    action: str | None = None


class BackfillOut(BaseModel):
    """Ход загрузки истории на карточке канала.

    ПОЧЕМУ ЗДЕСЬ ЧИСЛА, А НЕ ОДНО СМЕЩЕНИЕ. Было два поля: состояние и
    «сколько чатов позади». На единственный вопрос, который про эту загрузку
    задают — «сколько осталось», — они не отвечали: «300 чатов» одинаково
    выглядит и в начале часового прогона, и в конце. Теперь воркер сперва
    пересчитывает чаты, поэтому появился знаменатель.

    Все числа необязательные: прогон, начатый до выкатки, знает только
    смещение, и карточка обязана пережить это молча.
    """

    status: str  # running | stopped | failed | idle
    #: Сколько чатов уже позади: разобранные плюс сорвавшиеся. Прежнее имя
    #: поля сохранено намеренно: смысл тот же, а переименование сломало бы
    #: карточку в тот момент, когда прогон уже идёт. По этому числу движется
    #: полоса хода.
    chats_offset: int | None = None
    #: Сколько диалогов реально загружено — числитель подписи «N из M». Может
    #: быть меньше `chats_offset`: пустой чат и чат старее выбранной глубины
    #: остаются позади, но диалогом не становятся.
    loaded: int | None = None
    #: Сколько всего чатов насчитала перепись. ``None`` — перепись не
    #: закончилась или её прервали: показываем ход без знаменателя, а не
    #: выдуманное число.
    total: int | None = None
    #: Чаты, которые не удалось загрузить: повтор возьмёт их снова.
    failed_chats: int | None = None
    #: Сколько диалогов ушло операторам во «Входящие» (свежее непрочитанное).
    queued: int | None = None
    #: Глубина прогона: all | since_connect.
    depth: str | None = None
    #: census — идёт перепись чатов, loading — идёт загрузка, stopped —
    #: остановлено человеком.
    phase: str | None = None


class AvitoAccountOut(BaseModel):
    id: uuid.UUID
    title: str
    avito_user_id: int
    status: str  # active | needs_reauth | disabled
    token_expires_at: datetime
    #: Канал подключён своими ключами (а не через согласие). От этого зависит,
    #: чем чинить отказ: ключи постоянные, поэтому «Повторить сейчас» имеет
    #: смысл, а «Переподключить» через согласие — нет (других ключей не бывает).
    own_keys: bool = False
    #: Служебная заглушка регрессионного набора (`app/cli.py seed-smoke`), а не
    #: канал заказчика. Карточка обязана это сказать: у заглушки нет токена
    #: Авито вовсе, поэтому «Токен: активен, до 2036 года» и красное
    #: «Webhook: сбой» на ней читаются как рабочий канал со сломанной подпиской.
    #: Ровно так её и прочитали на боевой системе 11 августа.
    is_service: bool = False
    #: В какой лид-центр уходят заявки этого канала: `bt` / `kp` / `mnc`.
    #: `None` — не выбран, и тогда канал заявки НЕ отдаёт (`services/leads.py`).
    lead_src_key: str | None = None
    #: Поля заявки. Отдаются, чтобы экран показывал заполненное, а не пустые поля
    #: при непустой базе — иначе оператор впишет заново то, что уже стоит.
    lead_origin: str | None = None
    lead_partner_number: str | None = None
    review_url: str | None = None
    webhook: WebhookOut
    #: Строка «Токен …» целиком: три состояния вместо одной даты. Поле
    #: ОБЯЗАТЕЛЬНОЕ — сервер заполняет его всегда, и экрану не нужна ветка «а
    #: если состояния нет»: такая ветка неминуемо повторила бы старое правило
    #: «меньше суток — жёлтым», ради отмены которого всё и написано. Само
    #: `token_expires_at` выше оставлено (на него смотрят прежние сборки), но
    #: пугать человека по нему больше не нужно.
    token: TokenOut
    backfill: BackfillOut
    bot_id: uuid.UUID | None = None
    created_at: datetime | None = None
    #: Завели новый канал (True) или обновили ключи у уже подключённого (False).
    #: Заполняется только ручкой подключения — в списке каналов смысла не имеет.
    created: bool | None = None
    # Кто работает на канале (план 7.2, экран 11 §4.1): число и первые
    # аватарки прямо в карточке — при девяти каналах «а кто на этом?» первый
    # вопрос админа, и ради него не должно приходиться заходить внутрь.
    # Считаются только живые операторы: отключённый сотрудник в наборе
    # остаётся, но обращения не берёт, поэтому канал с одним таким снова
    # общий (правило совместимости — шапка app/api/routes/avito_accounts.py).
    # `count: 0` карточка подписывает «доступен всем операторам».
    operators: ChannelOperatorsSummary = Field(default_factory=ChannelOperatorsSummary)
    # Сводка за неделю (просьба заказчика от 7 августа): сколько обращений и
    # сколько из них НИКТО не ответил. На экране каналов сейчас видно только
    # «подключён / токен активен», а спрашивают другое: какой канал приносит
    # обращения, а какой мы теряем. Ответ жил в разделе статистики за двумя
    # фильтрами — и потому его не смотрели.
    stats: AccountStatsOut | None = None


class AccountStatsOut(BaseModel):
    """Неделя канала: два числа и форма."""

    total: int
    #: Обращения, где НИ ОДИН оператор не ответил ни разу. Это и есть
    #: «пропущенное обращение» из Jivo.
    missed: int
    #: По одному числу на день, от старого к новому. Длина всегда семь: день
    #: без обращений — ноль, а не пропуск, иначе столбики съезжают и форма
    #: недели врёт.
    daily: list[int]
    #: Сводка считается по часовому виду и отстаёт до часа. Говорим об этом
    #: прямо: «сейчас» показывает очередь «Входящие», и она живая.
    stale_minutes: int = 60


class PageOut(BaseModel):
    limit: int
    offset: int
    total: int


class AvitoAccountsPageOut(BaseModel):
    items: list[AvitoAccountOut]
    page: PageOut


async def _account_out(
    db: AsyncSession,
    account: AvitoAccount,
    redis: Redis,
    *,
    operators: ChannelOperatorsSummary | None = None,
    stats: account_stats.AccountSummary | None = None,
    ctx: channel_health.HealthContext | None = None,
) -> AvitoAccountOut:
    """Карточка канала целиком.

    ``ctx`` — рабочее окно и «сейчас», прочитанные ОДИН РАЗ на весь список.
    Без него девять каналов дали бы девять одинаковых запросов настроек
    (01 §5.1); одиночные ручки его не передают — там карточка одна.
    """
    context = ctx or await channel_health.load_context(db)
    backfill = await accounts_service.get_backfill_state(redis, account.id)
    webhook = await channel_health.webhook_health(db, redis, account, ctx=context)
    token = channel_health.token_health(
        account, await channel_health.read_journal(redis, account.id), now=context.now
    )
    return AvitoAccountOut(
        stats=(
            AccountStatsOut(total=stats.total, missed=stats.missed, daily=stats.daily)
            if stats is not None
            else None
        ),
        id=account.id,
        title=account.title,
        avito_user_id=account.avito_user_id,
        status=account.status,
        token_expires_at=account.token_expires_at,
        webhook=WebhookOut(
            status=webhook.status,
            url=webhook.url,
            # Строкой, как было: поле объявлено `str | None` и его формат —
            # часть публичного ответа с самого начала (01 §4.1).
            last_event_at=(
                webhook.last_event_at.isoformat() if webhook.last_event_at is not None else None
            ),
            state=webhook.state,
            reason=webhook.reason,
            message=webhook.message,
            headline=webhook.headline,
            detail=webhook.detail,
            silence_minutes=webhook.silence_minutes,
            quiet_minutes=webhook.quiet_minutes,
            rhythm_minutes=webhook.rhythm_minutes,
            rhythm_samples=webhook.rhythm_samples,
            rhythm_window_days=webhook.rhythm_window_days,
            threshold_minutes=webhook.threshold_minutes,
            checked_at=webhook.checked_at,
            check_result=webhook.check_result,
            action=webhook.action,
        ),
        token=TokenOut(
            state=token.state,
            reason=token.reason,
            message=token.message,
            headline=token.headline,
            detail=token.detail,
            expires_at=token.expires_at,
            expires_in_minutes=token.expires_in_minutes,
            auto_refresh_at=token.auto_refresh_at,
            last_refresh_at=token.last_refresh_at,
            last_refresh_ok=token.last_refresh_ok,
            last_error=token.last_error,
            failures=token.failures,
            action=token.action,
        ),
        backfill=BackfillOut(**backfill),
        bot_id=account.bot_id,
        created_at=account.created_at,
        operators=operators or ChannelOperatorsSummary(),
        own_keys=accounts_service.has_own_keys(account),
        is_service=bool(account.is_service),
        lead_src_key=account.lead_src_key,
        lead_origin=account.lead_origin,
        lead_partner_number=account.lead_partner_number,
        review_url=account.review_url,
    )


async def _operators_summary(db: AsyncSession, account_id: uuid.UUID) -> ChannelOperatorsSummary:
    """Свод операторов одной карточки — та же строка «Операторы: N» (11 §4.1)."""
    summary = (await operators_service.operators_summary(db, [account_id])).get(account_id)
    return ChannelOperatorsSummary.model_validate(summary) if summary else ChannelOperatorsSummary()


async def _get_account_or_404(db: AsyncSession, account_id: uuid.UUID) -> AvitoAccount:
    account = await db.get(AvitoAccount, account_id)
    if account is None:
        raise ApiError("not_found", status=404, message="Аккаунт Авито не найден")
    return account


def _assert_not_a_stub(account: AvitoAccount) -> None:
    """Служебную заглушку в строй не ставят — ни кнопкой, ни случайно.

    ЧТО СЛУЧИЛОСЬ НА БОЕВОЙ СИСТЕМЕ. `seed-smoke` заводит аккаунт-заглушку
    строго выключенным («никто не „включит“ заглушку случайно» — app/cli.py), и
    единственное, ради чего он существует, — непустой `conversations.account_id`
    у служебного диалога `SMOKE-CONV`. Токена Авито у него нет: в поле лежит
    строка ``smoke-account-has-no-avito-token``, а срок действия проставлен на
    десять лет вперёд.

    Дальше сработала арифметика, а не злой умысел. `enable` проверял ровно
    одно — жив ли токен по дате, — и у заглушки он «жив» до 2036 года. Канал
    стал ``active``, следом `register_webhook` сходил в Авито с той самой
    строкой вместо токена и записал ``webhook: failed``. На экране получилось
    полное собрание противоречий: зелёная точка, «Токен: активен», красное
    «Webhook: сбой», — а в колокольчике повисло «Приём сообщений остановился»,
    потому что для сторожа появился «работающий канал», в который никогда не
    придёт ни одного обращения.

    ПОЧЕМУ ОТКАЗ, А НЕ ТИХОЕ ИГНОРИРОВАНИЕ. Человек нажал кнопку и обязан
    узнать, что она не сработала и почему: молчаливый отказ читается как
    «включилось» и приводит к тому же экрану, только без объяснения.
    """
    if account.is_service:
        raise ApiError(
            "unprocessable",
            status=422,
            message=(
                "Это служебная заглушка регрессионного набора, а не канал Авито: "
                "токена у неё нет, обращения через неё не идут и идти не могут"
            ),
            details={"reason": "service_account"},
        )


# --- OAuth: начало флоу ------------------------------------------------------


async def _issue_state(
    redis: Redis, user: User, *, reconnect_account_id: uuid.UUID | None = None
) -> str:
    """Выпуск state делегирован сервису: тот же формат нужен кнопке
    «Переподключить» из центра уведомлений, а два места выпуска одноразового
    токена рано или поздно разъезжаются."""
    return await accounts_service.issue_oauth_state(
        redis, user.id, reconnect_account_id=reconnect_account_id
    )


# --- ключи приложения Авито (настраиваются из интерфейса) --------------------


class AvitoAppOut(BaseModel):
    """Что видит экран настроек. СЕКРЕТ СЮДА НЕ ПОПАДАЕТ никогда."""

    client_id: str
    #: Только факт: задан или нет. Показать секрет один раз — значит показать
    #: его каждому, кто заглянет через плечо.
    secret_set: bool
    api_base: str
    auth_url: str
    #: Смотрим на настоящий Авито или на встроенный имитатор.
    live: bool
    #: `db` — значения заданы из интерфейса, `env` — действуют настройки сервера.
    source: str
    #: Адрес возврата, который надо вписать в кабинете Авито. Не совпадёт —
    #: подключение будет падать без внятной причины.
    redirect_uri: str


class AvitoAppIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=200)
    #: `None` — «не меняли». Экран не показывает секрет, и без этого различия
    #: любое сохранение стирало бы его пустой строкой.
    client_secret: str | None = Field(default=None, max_length=500)
    api_base: str = Field(min_length=1, max_length=300)
    auth_url: str = Field(min_length=1, max_length=300)


def _app_out(config: avito_app.AvitoAppConfig) -> AvitoAppOut:
    return AvitoAppOut(
        client_id=config.client_id,
        secret_set=bool(config.client_secret),
        api_base=config.api_base,
        auth_url=config.auth_url,
        live=config.is_live,
        source=config.source,
        redirect_uri=avito_app.redirect_uri(),
    )


@router.get("/avito/app", response_model=AvitoAppOut)
async def get_avito_app(
    _user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AvitoAppOut:
    return _app_out(await avito_app.load(db))


@router.put("/avito/app", response_model=AvitoAppOut)
async def put_avito_app(
    body: AvitoAppIn,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AvitoAppOut:
    config = await avito_app.save(
        db,
        actor_id=user.id,
        client_id=body.client_id,
        client_secret=body.client_secret,
        api_base=body.api_base,
        auth_url=body.auth_url,
    )
    await write_audit(
        db,
        user_id=user.id,
        action="avito_app.updated",
        entity="settings",
        entity_id=avito_app.KEY,
        # Ключи в журнал НЕ пишем — ни целиком, ни кусками. Пишем то, что
        # действительно нужно при разборе: на что теперь смотрит система.
        details={"live": config.is_live, "api_base": config.api_base},
    )
    await db.commit()
    return _app_out(config)


class AvitoModeIn(BaseModel):
    live: bool


@router.post("/avito/app/mode", response_model=AvitoAppOut)
async def set_avito_mode(
    body: AvitoModeIn,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AvitoAppOut:
    """Настоящий Авито или встроенный имитатор — одним переключателем.

    Отдельно от `PUT /avito/app`: тот требует ключи приложения, а каналы
    подключаются своими ключами, и ключей приложения может не быть вовсе.
    Без этой ручки выйти из имитатора было нечем — что заказчик и увидел:
    любые ключи давали один и тот же выдуманный аккаунт.
    """
    config = await avito_app.set_live(db, actor_id=user.id, live=body.live)
    await write_audit(
        db,
        user_id=user.id,
        action="avito_app.updated",
        entity="settings",
        entity_id=avito_app.KEY,
        details={"live": config.is_live, "api_base": config.api_base},
    )
    await db.commit()
    return _app_out(config)


@router.delete("/avito/app", response_model=AvitoAppOut)
async def reset_avito_app(
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AvitoAppOut:
    """Вернуться к настройкам сервера — путь назад из «я тут напутал»."""
    config = await avito_app.reset(db)
    await write_audit(
        db,
        user_id=user.id,
        action="avito_app.reset",
        entity="settings",
        entity_id=avito_app.KEY,
    )
    await db.commit()
    return _app_out(config)


class AvitoAppCheckOut(BaseModel):
    ok: bool
    message: str


@router.post("/avito/app/check", response_model=AvitoAppCheckOut)
async def check_avito_app(
    _user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AvitoAppCheckOut:
    """Проверить связь ДО подключения канала.

    Смысл кнопки в том, чтобы ошибка в ключах всплыла здесь, а не посреди
    OAuth: там человек уже ушёл на сайт Авито, вернулся с отказом и не знает,
    что именно не так — ключ, адрес или права приложения.

    Проверяем самым дешёвым способом: просим у Авито токен приложения. Он
    отвечает на пару ключей и не требует ни согласия, ни аккаунта.
    """
    config = await avito_app.ensure_fresh(db)
    if not config.client_id or not config.client_secret:
        return AvitoAppCheckOut(ok=False, message="Ключи не заданы")
    try:
        await (await AvitoClient.fresh(db)).client_credentials_token()
    except AvitoUnavailable:
        return AvitoAppCheckOut(ok=False, message="Не удалось связаться с Авито — проверьте адрес")
    except AvitoApiError as exc:
        return AvitoAppCheckOut(ok=False, message=f"Авито не принял ключи: {exc}")
    where = "настоящий Авито" if config.is_live else "встроенный имитатор"
    return AvitoAppCheckOut(ok=True, message=f"Связь есть, отвечает {where}")


@router.get("/avito/connect-url", response_model=ConnectUrlOut)
async def avito_connect_url(
    user: User = Depends(require_permission("accounts:manage")),
    redis: Redis = Depends(get_redis),
) -> ConnectUrlOut:
    state = await _issue_state(redis, user)
    return ConnectUrlOut(url=build_authorize_url(state))


@router.post("/avito-accounts/{account_id}/reconnect", response_model=ConnectUrlOut)
async def avito_reconnect(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ConnectUrlOut:
    account = await _get_account_or_404(db, account_id)
    state = await _issue_state(redis, user, reconnect_account_id=account.id)
    return ConnectUrlOut(url=build_authorize_url(state))


# --- OAuth: callback ---------------------------------------------------------


def _settings_redirect(query: str) -> RedirectResponse:
    """Back to the SPA, never to the API origin.

    Авито возвращает человека в браузер по нашему callback'у, который живёт на
    origin'е API; фронт в dev — на :5173, в проде — на своём домене. Поэтому
    Location строится от FRONTEND_BASE_URL (05 §4), а не от пути API.
    """
    return RedirectResponse(
        f"{settings.frontend_base}{SETTINGS_ACCOUNTS_PATH}?{query}", status_code=302
    )


class ConnectLinkOut(BaseModel):
    url: str
    #: Сколько часов ссылка будет жить. Показывается рядом: человек должен
    #: понимать, что отправленная в мессенджер ссылка не вечная.
    expires_in_hours: int


class ConnectByKeysIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=500)
    #: Код источника («В95»), который проставится в заявке. Задаётся ПРЯМО ЗДЕСЬ
    #: по просьбе владельца 27.08: «при добавлении аккаунта сразу присвоить ему
    #: источник».
    #:
    #: ⚠ ПОЧЕМУ ОДНИМ ЗАПРОСОМ, А НЕ PATCH'ем СЛЕДОМ. Между созданием канала и
    #: правкой источника аккаунт уже живой: подписка на события оформлена, и
    #: первое обращение может прийти в эту же секунду. Заявка по нему ушла бы
    #: без кода источника — молча и невосстановимо. Тем же запросом такого окна
    #: нет вовсе.
    #:
    #: Пустая строка и None равны «не задавали»: у уже подключённого аккаунта
    #: повторное подключение ключами не должно стирать проставленный код.
    lead_origin: str | None = Field(default=None, max_length=32)
    #: «Да, я знаю, что на этом аккаунте стоит ЧУЖАЯ подписка, и забираю канал».
    #:
    #: По умолчанию False, и это главное в поле. Авито держит на аккаунт ровно
    #: одну подписку на события: подключение аккаунта, который сейчас
    #: обслуживает Jivo, отбирает канал у неё молча и мгновенно. Пока флага нет,
    #: такое подключение отвечает 409 и называет чужие адреса — см.
    #: `accounts_service.connect_with_keys`.
    #:
    #: Флаг про ОДНО нажатие: он не хранится и следующий раз спросится заново.
    takeover_confirmed: bool = False


@router.post("/avito-accounts/connect", response_model=AvitoAccountOut, status_code=201)
async def connect_account_by_keys(
    body: ConnectByKeysIn,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    """Подключить аккаунт ПО КЛЮЧАМ — без согласия, переходов и адреса возврата.

    РЕШЕНИЕ ЗАКАЗЧИКА: «давай так, чтобы для привязки нам был нужен только
    Client Secret и ID». Аккаунтов девять, пароли от них у разных людей, и
    «пришлите ключи» — просьба, которую можно выполнить, не отдавая пароль.

    Ошибки Авито отдаём человеческим текстом, а не пробрасываем как есть:
    сюда приходит владелец бизнеса, и «HTTP 401» ему ничего не говорит. Три
    случая, которые он реально встретит: ключи не те, приложению не выдали
    прав на мессенджер, Авито недоступен.

    ЧЕТВЁРТЫЙ СЛУЧАЙ, И ОН ОПАСНЕЕ ТРЁХ ПРЕДЫДУЩИХ (SCEN-32): на аккаунте уже
    стоит ЧУЖАЯ подписка на события. Авито держит на аккаунт ровно одну, и
    подключение молча забирало канал у той системы, которая его сейчас
    обслуживает, — то есть у работающего Jivo с тринадцатью диспетчерами.
    Теперь такое подключение отвечает 409 и требует подтверждения
    (`takeover_confirmed`); саму проверку делает `connect_with_keys` ДО первой
    записи в базу.
    """
    try:
        account, created = await accounts_service.connect_with_keys(
            db,
            redis,
            client_id=body.client_id.strip(),
            client_secret=body.client_secret.strip(),
            takeover_confirmed=body.takeover_confirmed,
        )
    except AvitoAuthError:
        raise ApiError(
            "unprocessable",
            status=422,
            message="Авито не принял эти ключи. Проверьте Client ID и Client Secret",
            details={"reason": "bad_keys"},
        ) from None
    except AvitoUnavailable:
        # ОТДЕЛЬНОЙ ВЕТКОЙ И ВЫШЕ ОБЩЕЙ. `AvitoUnavailable` — наследник
        # `AvitoApiError`, и без этой ветки обрыв сети объяснялся бы человеку
        # текстом про права мессенджера. Раньше здесь стоял `except OSError`,
        # который не ловил ничего: исключения httpx от OSError не наследуются,
        # и «Авито недоступен» доезжало как «Внутренняя ошибка сервера».
        raise ApiError(
            "bad_gateway",
            status=502,
            message="Не удалось связаться с Авито — попробуйте ещё раз через минуту",
            details={"reason": "unreachable"},
        ) from None
    except AvitoApiError as exc:
        raise ApiError(
            "bad_gateway",
            status=502,
            message=(
                "Авито принял ключи, но отказал в доступе к мессенджеру. "
                "Проверьте, что приложению выданы права messenger:read и messenger:write"
            ),
            details={"reason": "avito_error", "detail": str(exc)},
        ) from None

    # ⚠ ИСТОЧНИК СТАВИМ ДО АУДИТА И ДО ОТВЕТА, но ПОСЛЕ успешного подключения:
    # раньше писать некуда, позже — уже поздно (см. комментарий в ConnectByKeysIn).
    источник = (body.lead_origin or "").strip()
    if источник:
        account.lead_origin = источник
        await db.flush()

    await write_audit(
        db,
        user_id=user.id,
        action="account.connected",
        entity="avito_account",
        entity_id=str(account.id),
        details={
            "avito_user_id": account.avito_user_id,
            "created": created,
            "by_keys": True,
            # Отбирали ли канал у чужой подписки. Разбору «когда именно Jivo
            # ослеп» отвечает ровно это поле и ничто другое: сам факт
            # подключения выглядит одинаково в обоих случаях.
            "takeover_confirmed": body.takeover_confirmed,
        },
    )
    await db.commit()

    # Подписка на входящие — сразу, как и при подключении через согласие.
    #
    # ВЫЗОВ ОБЯЗАН БЫТЬ ЗАЩИЩЁН, и вот почему. Он стоит ПОСЛЕ commit: аккаунт
    # уже сохранён. Если здесь вылетит исключение, человек получит 500
    # «Внутренняя ошибка сервера» и решит, что подключение не прошло, — а оно
    # прошло, и канал обнаружится сам собой после перезагрузки страницы.
    # Внутри `register_webhook` ловятся только (AvitoApiError, OSError,
    # DecryptError); сетевые сбои теперь тоже приходят как AvitoApiError, но
    # полагаться на полноту чужого списка здесь нельзя — цена промаха
    # несоразмерна.
    subscribed = False
    try:
        subscribed = await accounts_service.register_webhook(account, redis)
    except Exception:  # noqa: BLE001 — подключение уже состоялось, ронять нечего
        log.warning("account.webhook_subscribe_failed", account_id=str(account.id), exc_info=True)
        # СОСТОЯНИЕ ПОДПИСКИ ПИШЕМ И ЗДЕСЬ. `register_webhook` помечает провал
        # сам, но только для тех ошибок, которые ловит внутри; всё, что
        # прилетело мимо его списка, оставляло на карточке прежнюю надпись —
        # то есть «не зарегистрирован» у канала, где на самом деле только что
        # ПЫТАЛИСЬ зарегистрировать и не смогли. Разница для человека
        # решающая: одно читается как «ещё не делали», другое как «сделали и
        # не вышло».
        await accounts_service.set_webhook_state(
            redis, account.id, "failed", accounts_service.webhook_url_for(account)
        )

    if not subscribed:
        # ПРОВАЛ ПОДПИСКИ БОЛЬШЕ НЕ ПРОГЛАТЫВАЕТСЯ (SCEN-35).
        #
        # Что было: ответ 201, зелёное «Аккаунт подключён · Новые обращения
        # пойдут в Чаты» — и ни одного обращения. Канал подключён по-настоящему
        # (токены на месте, карточка в списке), но подписки на события у него
        # нет, и Авито нам ничего не шлёт. Хуже сочетания не придумать: система
        # уверяет, что работает, а клиенты пишут в пустоту. Узнают об этом по
        # звонку клиента через сутки.
        #
        # ПОЧЕМУ НЕ ОШИБКА ОТВЕТА. Подключение СОСТОЯЛОСЬ — канал сохранён и
        # виден в списке. Ответить «не подключилось» значило бы соврать в
        # другую сторону и позвать человека нажимать ещё раз.
        #
        # ПОЧЕМУ ЦЕНТР УВЕДОМЛЕНИЙ, А НЕ ПОЛЕ В ОТВЕТЕ. Поле в ответе уже есть
        # (`webhook.status == "failed"`), и его показывает карточка канала — но
        # мастер закрывается зелёным тостом, и на карточку человек посмотрит
        # не раньше, чем что-то заподозрит. Уведомление вида `webhook.lost`
        # критично, поднимает красную плашку поверх экрана и несёт кнопку
        # «Перерегистрировать» — то есть говорит о беде тогда же, когда она
        # случилась, и тут же даёт её починить.
        await accounts_service.announce_webhook_lost(db, redis, account)

    out = await _account_out(db, account, redis, operators=await _operators_summary(db, account.id))
    # ГЛАВНОЕ ПОЛЕ ЭТОГО ОТВЕТА, ХОТЬ И ПОСЛЕДНЕЕ.
    #
    # Аккаунты ищутся по `avito_user_id`, и если он совпал с уже подключённым,
    # строка ОБНОВЛЯЕТСЯ, а не заводится новая. Название при этом остаётся
    # прежним — так задумано, чтобы переименование канала не сбрасывалось.
    #
    # Пока наружу ехал один и тот же ответ, это выглядело как успех: человек
    # подключал второй аккаунт своими ключами, видел зелёное «подключён» и
    # чужое название, а список каналов не рос. Ровно отсюда «я могу подключить
    # только 1 аккаунт»: при входе по ключам пара принадлежит ТОМУ аккаунту,
    # где заведено приложение, поэтому одни ключи на девять аккаунтов девять
    # раз попадут в один и тот же канал.
    out.created = created
    return out


@router.post("/avito/connect-link", response_model=ConnectLinkOut)
async def create_connect_link(
    user: User = Depends(require_permission("accounts:manage")),
    redis: Redis = Depends(get_redis),
) -> ConnectLinkOut:
    """Ссылка «подключите свой аккаунт Авито» — для того, у кого от него пароль.

    ЗАЧЕМ. Подключить аккаунт может только администратор LeadChat. Но пароли
    от аккаунтов Авито у разных людей, и владелец системы их у себя не держит
    — и правильно делает. До сих пор это означало «пришлите мне пароль», то
    есть ровно то, чего делать нельзя.

    Заказчик сказал, как это было устроено у прежней системы: «мы просто
    давали ссылку и всё». Теперь так же: ссылку отправляют человеку, он
    открывает её, входит в Авито под своей учётной записью и разрешает
    доступ. В LeadChat ему заходить не нужно, и доступа к нему не требуется.
    """
    token, ttl = await accounts_service.issue_connect_link(redis, actor_id=user.id)
    return ConnectLinkOut(
        url=f"{settings.public_base}/connect/{token}",
        expires_in_hours=ttl // 3600,
    )


@router.get("/avito/connect/{token}", include_in_schema=False)
async def follow_connect_link(
    token: str,
    redis: Redis = Depends(get_redis),
) -> RedirectResponse:
    """Публичная: по ней приходит человек с паролем от аккаунта Авито.

    Права заменяет сам токен — одноразовый и живущий сутки, как одноразовая
    ссылка приглашения сотрудника (01 §13). Ошибки отдаём редиректом, а не
    JSON: на той стороне человек в браузере, и он не должен увидеть
    `{"error": …}` вместо объяснения.
    """
    # ⚠ ЧИТАЕМ, НЕ ГАСЯ. Это публичный GET, а ссылку отправляют человеку в мессенджер —
    # значит первым по ней приходит превью-бот, сканер почты или префетч браузера.
    # Гашение здесь убивало ссылку ДО того, как её нажмут, а перевыпустить её тот, кому
    # она адресована, не может: доступа в LeadChat у него нет. Разбор — у
    # `peek_connect_link`; гасим в callback'е, вплотную к записи аккаунта.
    issued_by = await accounts_service.peek_connect_link(redis, token)
    if issued_by is None:
        log.warning("connect_link.invalid")
        return _settings_redirect("error=connect_link_expired")

    state = await accounts_service.issue_oauth_state(redis, issued_by, connect_link_token=token)
    return RedirectResponse(build_authorize_url(state), status_code=302)


@router.get("/avito/callback", include_in_schema=False)
@router.post("/avito/callback")
async def avito_callback(
    code: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RedirectResponse:
    """Публичный (приходит браузером админа с Авито). Право заменяет
    одноразовый state-токен (01 §13); ошибки — редиректом, без envelope:
    на той стороне человек в браузере, а не API-клиент."""
    if not code or not state:
        return _settings_redirect("error=oauth_failed")

    raw_state = await redis.getdel(f"oauth_state:{state}")
    if not raw_state:
        log.warning("oauth.callback_bad_state")
        return _settings_redirect("error=oauth_failed")
    try:
        state_data: dict[str, Any] = json.loads(raw_state)
    except ValueError:
        # ⚠ ПУСТОЙ СЛОВАРЬ ЗДЕСЬ МОЛЧА СНИМАЛ ЗАСЛОН (разбор 03.09).
        #
        # Из `{}` получались `initiator_id=None` и `reconnect_account_id=None`,
        # а по ним ниже проверяется, тот ли аккаунт Авито авторизовал админ.
        # То есть испорченный `state` не отказывал, а ОТКЛЮЧАЛ проверку — и не
        # писал об этом ни строки, хотя все соседние отказы этого обработчика
        # пишут.
        #
        # Отказываем так же, как при неизвестном `state` двумя строками выше.
        # Место выбрано намеренно: ДО гашения одноразовой ссылки, иначе
        # вернулась бы гонка «двое по одной ссылке».
        log.warning("oauth.callback_bad_state_format")
        return _settings_redirect("error=oauth_failed")
    initiator_id = state_data.get("user_id")
    reconnect_account_id = state_data.get("reconnect_account_id")
    connect_link_token = state_data.get("connect_link_token")

    # Из базы, а не из кэша процесса — см. AvitoClient.fresh.
    client = await AvitoClient.fresh(db)
    try:
        tokens = await client.exchange_code(code)
        profile = await client.get_self(tokens["access_token"])
    except (AvitoApiError, OSError) as exc:
        log.warning("oauth.callback_failed", error=str(exc))
        return _settings_redirect("error=oauth_failed")

    # ⚠ ГАСИМ ССЫЛКУ ЗДЕСЬ, И ЭТО ПОСЛЕДНИЙ РУБЕЖ ОТ ГОНКИ.
    #
    # Позади остались все шаги, которые могут отказать не по вине человека: страница
    # согласия Авито, обмен кода, `get_self` — сеть на каждом. Раньше ссылка сгорала
    # ДО них, на публичном GET, и любой сбой оставлял человека с мёртвой ссылкой,
    # которую он не может перевыпустить.
    #
    # GETDEL атомарен: пустой ответ означает «параллельный переход уже подключил», и
    # тогда мы НИЧЕГО не пишем. Двое, открывшие одну ссылку, дойдут до согласия оба, но
    # аккаунт запишет ровно один — второй просто выбросит полученный токен.
    #
    # Между этой строкой и записью аккаунта не должно появиться ни одной проверки,
    # иначе дефект вернётся с другой стороны.
    if connect_link_token is not None:
        if await accounts_service.consume_connect_link(redis, connect_link_token) is None:
            log.warning("connect_link.already_used")
            return _settings_redirect("error=connect_link_expired")

    if reconnect_account_id:
        account = await db.get(AvitoAccount, uuid.UUID(reconnect_account_id))
        if account is None:
            return _settings_redirect("error=oauth_failed")
        if account.avito_user_id != int(profile["id"]):
            # админ авторизовал другой аккаунт Авито — ничего не пишем (01 §4.4)
            log.warning("oauth.account_mismatch", account_id=str(account.id))
            return _settings_redirect("error=account_mismatch")
        accounts_service.apply_token_response(account, tokens)
        created = False
    else:
        account, created = await accounts_service.upsert_account(db, profile, tokens)

    await write_audit(
        db,
        user_id=uuid.UUID(initiator_id) if initiator_id else None,
        action="account.connected",
        entity="avito_account",
        entity_id=str(account.id),
        details={
            "avito_user_id": account.avito_user_id,
            "created": created,
            "reconnect": bool(reconnect_account_id),
        },
    )
    await db.commit()

    # Провал регистрации вебхука не откатывает подключение: аккаунт сохранён,
    # статус failed виден в списке, перерегистрация — кнопкой (01 §4.7).
    #
    # НО И МОЛЧАТЬ О НЁМ НЕЛЬЗЯ (SCEN-35). Итог этого вызова здесь просто
    # выбрасывался, а человек уезжал редиректом `connected=1` — то есть
    # «подключено» ему говорили в обоих случаях, и в том, где обращения не
    # пойдут, тоже. Теперь провал называется дважды: красной плашкой центра
    # уведомлений с кнопкой починки и меткой в адресе возврата.
    subscribed = await accounts_service.register_webhook(account, redis)
    if not subscribed:
        await accounts_service.announce_webhook_lost(db, redis, account)
    # ИСТОРИЯ ГРУЗИТСЯ ЦЕЛИКОМ (решение владельца 17.08 — разворот решения от
    # 8 августа). Контекст изменился: каналы параллельно работали в Jivo, и при
    # переезде на LeadChat прошлая переписка аккаунта нужна диспетчерам —
    # клиент из Jivo-эпохи пишет «мы же договаривались», и без истории человек
    # слеп. Механика вся прежняя (backfill_account: резюмируемая, с прогрессом,
    # HISTORY_ALL): история ложится ЗАКРЫТЫМИ диалогами — очередь «Входящих»,
    # бейджи и боты её не видят; живые экраны обновляет кадр account:backfill.
    await accounts_service.enqueue_backfill(account.id)

    log.info("account.connected", account_id=str(account.id), created=created, webhook=subscribed)
    # Метка в адресе — для страницы настроек: она открывается сразу после
    # возврата и может сказать про подписку в тот же миг, не дожидаясь, пока
    # человек присмотрится к карточке. Само подключение состоялось в обоих
    # случаях, поэтому `connected=1` остаётся.
    return _settings_redirect("connected=1" if subscribed else "connected=1&webhook=failed")


# --- список и управление -----------------------------------------------------


@router.get("/avito-accounts", response_model=AvitoAccountsPageOut)
async def list_avito_accounts(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("accounts:read")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountsPageOut:
    total = await accounts_service.count_accounts(db)
    stmt = (
        select(AvitoAccount)
        .order_by(AvitoAccount.created_at, AvitoAccount.id)
        .limit(limit)
        .offset(offset)
    )
    accounts = (await db.execute(stmt)).scalars().all()
    # Один запрос на всю страницу, а не по запросу на карточку: девять каналов
    # сегодня, но N+1 в списке — тот самый запрет 01 §5.1.
    summaries = await operators_service.operators_summary(db, [account.id for account in accounts])
    # Сводка — тоже ОДНИМ запросом на всю страницу: девять каналов не должны
    # означать девять походов в базу ради девяти строчек.
    weekly = await account_stats.summaries(db)
    # Рабочее окно и «сейчас» — тоже один раз на страницу: строки про токен и
    # про тишину считаются в тех же часах, в которых работают люди, и читать
    # настройку заново под каждую карточку незачем.
    # Карту последних входящих готовим СРАЗУ НА ВСЮ СТРАНИЦУ: раньше её
    # спрашивала каждая карточка, и на тридцати пяти каналах это давало 0,8 с
    # при медиане прочих ручек 26 мс (замер 31.08). Комментарии выше запрещают
    # ровно это, но одна ветка запрет обходила.
    ctx = await channel_health.load_context(db, account_ids=[a.id for a in accounts])
    items = [
        await _account_out(
            db,
            account,
            redis,
            operators=ChannelOperatorsSummary.model_validate(
                summaries.get(account.id) or {"count": 0, "preview": []}
            ),
            stats=weekly.get(account.id) or account_stats.empty(account.id),
            ctx=ctx,
        )
        for account in accounts
    ]
    return AvitoAccountsPageOut(items=items, page=PageOut(limit=limit, offset=offset, total=total))


@router.post("/avito-accounts/{account_id}/backfill")
async def start_backfill(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Загрузить историю канала (кнопка на карточке; резюмируемо, дедуп)."""
    account = await _get_account_or_404(db, account_id)
    if account.status != "active":
        raise ApiError("conflict", "Канал не активен — сначала подключите его", status=409)
    state = await accounts_service.get_backfill_state(redis, account.id)
    if state.get("status") == "running":
        # второй клик поверх живого прогона = два параллельных обхода одних
        # чатов: прыгающие цифры, ложные failed_chats и убитая точка
        # возобновления (аудит свежего слоя 17.08, находка №8)
        raise ApiError("conflict", "Загрузка уже идёт — дождитесь или остановите её", status=409)
    # фаза queued ДО enqueue: кнопка гаснет сразу, окно двойного клика закрыто
    await accounts_service.mark_backfill_queued(redis, account.id)
    # dedupe=False: осознанный клик человека обязан начать прогон даже в
    # часовое окно keep_result после прежнего (см. докстринг helper'а)
    await accounts_service.enqueue_backfill(account.id, dedupe=False)
    ok = True
    await write_audit(
        db,
        user_id=user.id,
        action="account.backfill_started",
        entity="account",
        entity_id=str(account.id),
        details={"queued": ok},
    )
    await db.commit()
    return {"queued": ok, "backfill": await accounts_service.get_backfill_state(redis, account.id)}


@router.post("/avito-accounts/{account_id}/disable", response_model=AvitoAccountOut)
async def disable_avito_account(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    account = await _get_account_or_404(db, account_id)
    if account.status != "disabled":
        # снимаем вебхук на стороне Авито; недоступность Авито не блокирует (01 §4.5)
        await accounts_service.unregister_webhook(account, redis)
        account.status = "disabled"
        # И СТИРАЕМ ПЕРЕПИСКУ — решение заказчика от 8 августа: аккаунты Авито
        # у компании постоянно меняются, и переписка отключённого канала не
        # нужна никому. Раньше карточка обещала «история доступна для чтения»;
        # теперь отключение — это и есть расставание с каналом.
        removed = await accounts_service.purge_history(db, account.id)
        # следы прогона истории — тоже: иначе повторная загрузка после
        # переподключения молча пропустит стёртые чаты (находка №7, critical)
        await accounts_service.clear_backfill_state(redis, account.id)
        await write_audit(
            db,
            user_id=user.id,
            action="account.disabled",
            entity="avito_account",
            entity_id=str(account.id),
            details={"conversations_removed": removed},
        )
        await db.commit()
    return await _account_out(
        db, account, redis, operators=await _operators_summary(db, account.id)
    )


class AvitoAccountPatchIn(BaseModel):
    """Что можно изменить у канала. `None` — «не трогали»."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=80)
    #: В какой лид-центр уходят заявки этого канала: `bt` / `kp` / `mnc`.
    #: Пустая строка — снять выбор, и тогда канал заявки не отдаёт вовсе
    #: (`app/services/leads.py`). Девять аккаунтов могут вести разные
    #: направления, общего ответа нет — выбирает человек.
    lead_src_key: str | None = Field(default=None, max_length=8)
    #: Источник («В95») — наша пометка, уходит в «Комментарий Партнера» заявки: по
    #: ней в лид-центре видно, откуда пришёл клиент.
    #:
    #: ⚠ КОЛОНКА БЫЛА С МИГРАЦИИ 0040, А ЗАПОЛНИТЬ ЕЁ БЫЛО НЕЧЕМ. Ни в схеме правки,
    #: ни во фронте она не упоминалась ни разу — только правкой в базе руками.
    #: Владелец 14 августа: «я не могу указать источник, который будет использовать
    #: при автосоздании заявки, и ссылку на отзыв».
    lead_origin: str | None = Field(default=None, max_length=32)
    #: Номер партнёра, КАК ЕГО ЗНАЮТ ЛЮДИ («7», «723»). По нему же решается галочка
    #: «Отзыв» в заявке (`lead_flags.wants_review`).
    lead_partner_number: str | None = Field(default=None, max_length=16)
    #: Короткая ссылка на отзыв. Вписывается руками один раз: сокращатель — чужой
    #: сервис, и ставить создание заявки в зависимость от его доступности незачем.
    review_url: str | None = Field(default=None, max_length=500)


@router.patch("/avito-accounts/{account_id}", response_model=AvitoAccountOut)
async def rename_avito_account(
    account_id: uuid.UUID,
    body: AvitoAccountPatchIn,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    """Переименовать канал (просьба заказчика: «нужна возможность редактирования»).

    Имя канала приходит из Авито как есть — «! Парт - 7 / Ист - В43 МНЧ !».
    Это рабочее название учётной записи, и в списках, фильтрах и статистике оно
    читается плохо. У Jivo переименования нет вовсе, и заказчик живёт с этими
    строками; повторять чужое отсутствие функции — не решение.

    На стороне Авито ничего не меняется: имя наше и живёт только у нас.
    Переподключение канала его не затрёт — `upsert_account` присваивает
    название только при создании.
    """
    account = await _get_account_or_404(db, account_id)
    if body.title is not None:
        was = account.title
        account.title = body.title.strip()
        if account.title != was:
            await write_audit(
                db,
                user_id=user.id,
                action="account.renamed",
                entity="avito_account",
                entity_id=str(account.id),
                details={"from": was, "to": account.title},
            )
    # Три поля заявки правятся одинаково: пустая строка снимает значение, иначе
    # пишем как есть. Журнал — одной строкой на правку с тем, что было и стало
    # (проверка 24.09): от партнёра зависит галочка «Отзыв», от источника —
    # «Комментарий Партнера» заявки, и «кто и когда поменял» обязано иметь
    # ответ. `lead_src_key` пишется отдельно ниже: его смена МЕНЯЕТ АДРЕСАТА
    # заявок.
    changes: dict[str, dict[str, str | None]] = {}
    for field in ("lead_origin", "lead_partner_number", "review_url"):
        submitted = getattr(body, field)
        if submitted is None:
            continue
        was, now = getattr(account, field), submitted.strip() or None
        if now != was:
            changes[field] = {"from": was, "to": now}
        setattr(account, field, now)
    if changes:
        await write_audit(
            db,
            user_id=user.id,
            action="account.lead_fields_changed",
            entity="avito_account",
            entity_id=str(account.id),
            details={"changes": changes},
        )

    if body.lead_src_key is not None:
        # Пустая строка — осознанное «снять выбор». С 14.08 это НЕ останавливает
        # заявки: направление сперва решает объявление (`lead_direction`), и без
        # запасного выбора придерживаются только диалоги, где оно не определилось.
        # Такие видны в журнале с причиной, называющей оба несработавших пути.
        choice = body.lead_src_key.strip().lower() or None
        if choice is not None and choice not in SRC_KEYS:
            raise ApiError("bad_value", "Неизвестный лид-центр", status=422)
        if account.lead_src_key != choice:
            await write_audit(
                db,
                user_id=user.id,
                action="account.lead_src_changed",
                entity="avito_account",
                entity_id=str(account.id),
                details={"from": account.lead_src_key, "to": choice},
            )
            account.lead_src_key = choice
    await db.commit()
    return await _account_out(
        db, account, redis, operators=await _operators_summary(db, account.id)
    )


@router.post("/avito-accounts/{account_id}/register-webhook", response_model=AvitoAccountOut)
async def register_avito_webhook(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    """Подписаться на входящие заново, не переподключая канал через Авито.

    ЗАЧЕМ ОТДЕЛЬНАЯ КНОПКА. Подписка ставится сама при подключении, но её
    состояние живёт в оперативной памяти сервера: после его перезапуска живой
    канал подписан «не зарегистрирован», и вернуть надпись к правде было
    нечем — приходилось переподключать канал целиком через согласие Авито,
    то есть ходить в браузер и заново отдавать доступ.

    Второй и более важный случай — параллельная работа с Jivo. Если после
    нашей отписки Авито оставит канал без подписки вообще, вернуть приём
    сообщений надо одним нажатием, а не заново проходить OAuth.
    """
    account = await _get_account_or_404(db, account_id)
    # Подписка заглушки — это и есть та красная строка «Webhook: сбой», из-за
    # которой всё началось: запрос уходит со строкой-заполнителем вместо
    # токена и не может не провалиться.
    _assert_not_a_stub(account)
    if account.status != "active":
        raise ApiError(
            "unprocessable",
            status=422,
            message="Подписаться можно только на включённом канале",
            details={"reason": "not_active"},
        )
    ok = await accounts_service.register_webhook(account, redis)
    await write_audit(
        db,
        user_id=user.id,
        action="account.webhook_registered" if ok else "account.webhook_register_failed",
        entity="avito_account",
        entity_id=str(account.id),
    )
    await db.commit()
    if not ok:
        raise ApiError(
            "bad_gateway",
            status=502,
            message="Авито не принял подписку — попробуйте ещё раз через минуту",
            details={"reason": "avito_unavailable"},
        )
    return await _account_out(
        db, account, redis, operators=await _operators_summary(db, account.id)
    )


class AccountHistorySizeOut(BaseModel):
    """Сколько переписки уйдёт вместе с каналом: два числа для подтверждения."""

    conversations: int
    messages: int


@router.get("/avito-accounts/{account_id}/history-size", response_model=AccountHistorySizeOut)
async def avito_account_history_size(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
) -> AccountHistorySizeOut:
    """Сколько диалогов и сообщений будет стёрто вместе с каналом.

    ЗАЧЕМ ОТДЕЛЬНАЯ РУЧКА, А НЕ ПОЛЕ В КАРТОЧКЕ. Оба необратимых действия над
    каналом («Отключить и стереть», «Удалить») стирают переписку, и
    подтверждение обязано называть её размер: «все диалоги и сообщения» — это
    ноль информации, за ним одинаково прячутся пустая ошибочная строка и год
    работы живого канала. Числа при этом нужны ровно в момент нажатия, а
    считать их для каждой карточки в списке дорого: `count(*)` по сообщениям
    канала — это проход по индексу диалогов на каждое открытие экрана, при
    девяти каналах девять таких проходов, и всё это ради подсказки, которую
    обычно никто не читает. Поэтому — по запросу, одним походом на клик.

    Право `accounts:manage`, а не `accounts:read`: числа спрашивает тот, кто
    собрался стирать. Руководителю, который видит карточки, они ни к чему.

    Числа приблизительны ровно в той мере, в какой между запросом и нажатием
    успеет прийти новое сообщение. Округлять или кэшировать поэтому нечего:
    порядок величины — это всё, что нужно человеку перед необратимым шагом.
    """
    await _get_account_or_404(db, account_id)  # 404 раньше, чем счёт по чужому id
    conversations = (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.account_id == account_id)
        )
    ).scalar_one()
    messages = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.account_id == account_id)
        )
    ).scalar_one()
    return AccountHistorySizeOut(conversations=int(conversations), messages=int(messages))


@router.delete("/avito-accounts/{account_id}", status_code=204)
async def delete_avito_account(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Response:
    """Убрать канал из списка совсем — ВМЕСТЕ С ЕГО ПЕРЕПИСКОЙ.

    ЗДЕСЬ СТОЯЛО «ТОЛЬКО ПУСТОЙ. Канал с перепиской удалять нельзя». Это была
    правда до 8 августа; отказ снят по решению заказчика (аккаунты Авито у
    компании меняются постоянно, история ушедшего не нужна никому), а
    докстрока осталась и три месяца обещала защиту, которой в коде нет.
    Администратор, читающий «Удалить» как «убрать ошибочно подключённую
    строку», уничтожал переписку живого канала. Ту же неправду фронт повторял
    за этой докстрокой своими словами.

    ЧЕМ ОТЛИЧАЕТСЯ ОТ «ОТКЛЮЧИТЬ И СТЕРЕТЬ». Переписку стирают оба, и в этом
    их путают. Разница в судьбе самого канала: отключение оставляет строку в
    списке — приём остановлен, включить можно одним нажатием, токены на
    месте. Удаление убирает строку совсем, и вернуть канал можно только
    заново пройдя согласие в Авито (или заново вбив ключи).

    Сколько именно переписки уйдёт — считает
    ``GET /avito-accounts/{id}/history-size``; подтверждение в интерфейсе
    называет эти числа до нажатия.

    Подписку снимаем ДО удаления. Иначе Авито продолжит слать сообщения на
    наш адрес по каналу, которого у нас больше нет, — и они будут молча
    падать в отстойник разбора.
    """
    account = await _get_account_or_404(db, account_id)

    # ПЕРЕПИСКА УХОДИТ ВМЕСТЕ С КАНАЛОМ.
    #
    # Здесь стоял отказ «в канале N диалогов — отключите вместо удаления», и он
    # был верен, пока история канала считалась ценностью. По решению заказчика
    # от 8 августа она ею не является: аккаунты Авито меняются постоянно, и
    # переписка ушедшего не нужна никому.
    #
    # Отказ снят, но действие осталось необратимым — предупреждает о нём
    # подтверждение в интерфейсе, и оно называет числа: диалоги и сообщения
    # берутся из `GET /avito-accounts/{id}/history-size` перед показом окна.
    # До 11 августа этот комментарий обещал то же самое, а в окне не было ни
    # одного числа — обещание закрыто ручкой выше, а не переписыванием текста.
    title = account.title
    if account.status != "disabled":
        await accounts_service.unregister_webhook(account, redis)
    removed = await accounts_service.purge_history(db, account_id)
    await accounts_service.clear_backfill_state(redis, account_id)
    await db.delete(account)
    await write_audit(
        db,
        user_id=user.id,
        action="account.deleted",
        entity="avito_account",
        entity_id=str(account_id),
        details={"title": title, "conversations_removed": removed},
    )
    await db.commit()
    return Response(status_code=204)


@router.post("/avito-accounts/{account_id}/refresh-token", response_model=AvitoAccountOut)
async def refresh_avito_token(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    """Обновить токен канала, не проходя OAuth заново (блок 8.3).

    ЗАЧЕМ ОТДЕЛЬНО ОТ «ПЕРЕПОДКЛЮЧИТЬ». Переподключение — это поход в Авито,
    вход под учёткой канала и подтверждение доступа: минуты и чужой пароль под
    рукой. Обновление токена не требует ни того, ни другого — у нас есть
    `refresh_token`, и вся операция это один запрос. Разница видна в момент,
    когда канал внезапно перестал отвечать: одно чинится за секунду, другое
    требует человека с доступом к аккаунту Авито.

    Плановое обновление и так делает планировщик за два часа до истечения
    (08 §6.1). Кнопка — для случаев, когда ждать нельзя: токен протух раньше
    срока, канал молчит, и надо проверить прямо сейчас.

    Отказ Авито означает, что доступ отозван по-настоящему: сервис сам
    переводит канал в `needs_reauth` и зовёт админов — здесь остаётся только
    честно сказать, что без переподключения не обойтись.
    """
    account = await _get_account_or_404(db, account_id)
    # Заглушке обновлять нечего: в поле токена лежит строка-заполнитель, и
    # поход в Авито с ней кончится отзывом доступа у канала, которого нет.
    _assert_not_a_stub(account)
    if account.status == "disabled":
        # Выключенный канал не обновляют: обновление токена — подготовка к
        # работе, а этот канал выключили НАМЕРЕННО, и «Отключить» у нас
        # необратимо стирает переписку. Раньше отказа не было, и кнопка
        # молча возвращала канал в строй (поймано на боевом 11 августа).
        raise ApiError(
            "unprocessable",
            status=422,
            message="Канал выключен — сначала включите его, потом обновляйте токен",
            details={"reason": "account_disabled"},
        )
    if account.status == "needs_reauth" and not accounts_service.has_own_keys(account):
        # Отказ уместен ТОЛЬКО для подключения через согласие: там refresh-токен
        # одноразовый, и отозванный не оживёт — нужен человек с доступом к
        # аккаунту Авито.
        #
        # Для канала на своих ключах отказывать не за что. `client_id` и
        # `client_secret` Авито выдаёт один раз и не меняет: ключи, которыми
        # канал работал вчера, действительны и сегодня. Повторный запрос токена
        # ими безопасен и почти всегда срабатывает — а до 11 августа кнопка
        # отвечала «обновлять нечего, нужно переподключение», хотя
        # переподключать было нечем и незачем.
        raise ApiError(
            "unprocessable",
            status=422,
            message="Доступ отозван — обновлять нечего, нужно переподключение",
            details={"reason": "needs_reauth"},
        )

    outcome = await _refresh_outcome(account, db, redis)
    if outcome != accounts_service.REFRESH_OK:
        # Исход, а не догадка по статусу (проверка 24.09): у канала на своих
        # ключах сбой Авито оставляет прежний статус, и False читалось то
        # «уже обновляется», то «отозвал доступ».
        _raise_refresh_failure(account, outcome)

    await write_audit(
        db,
        user_id=user.id,
        action="account.token_refreshed",
        entity="avito_account",
        entity_id=str(account.id),
        details={"by": "manual"},
    )
    await db.commit()
    return await _account_out(
        db, account, redis, operators=await _operators_summary(db, account.id)
    )


async def _refresh_outcome(account: AvitoAccount, db: AsyncSession, redis: Redis) -> str:
    """Исход обновления для ручки: сбой сети на обмене refresh (подключение
    через согласие) — тоже «Авито не ответил», а не 500."""
    try:
        return await accounts_service.refresh_outcome(account, db, redis)
    except (AvitoUnavailable, AvitoApiError, httpx.HTTPError) as exc:
        log.warning(
            "token.manual_refresh_unavailable",
            account_id=str(account.id),
            error=type(exc).__name__,
            status=getattr(exc, "status", None),
        )
        return accounts_service.REFRESH_UNAVAILABLE


def _raise_refresh_failure(account: AvitoAccount, outcome: str) -> NoReturn:
    """Честная причина неудачного обновления — одна на все ручки канала."""
    if outcome == accounts_service.REFRESH_BUSY:
        raise ApiError(
            "conflict",
            status=409,
            message="Токен уже обновляется — подождите несколько секунд",
            details={"reason": "refresh_in_progress"},
        )
    if outcome == accounts_service.REFRESH_REVOKED:
        raise ApiError(
            "unprocessable",
            status=422,
            message=(
                "Авито не принял ключи приложения — проверьте в кабинете Авито, "
                "не удалено ли и не отключено ли приложение"
                if accounts_service.has_own_keys(account)
                else "Авито отозвал доступ — требуется переподключение"
            ),
            details={"reason": "needs_reauth"},
        )
    raise ApiError(
        "avito_unavailable",
        status=502,
        message="Авито не ответил — повторите через минуту. Токены и ключи канала целы",
        details={"reason": "avito_unavailable"},
    )


@router.post("/avito-accounts/{account_id}/enable", response_model=AvitoAccountOut)
async def enable_avito_account(
    account_id: uuid.UUID,
    user: User = Depends(require_permission("accounts:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AvitoAccountOut:
    account = await _get_account_or_404(db, account_id)
    # ПЕРВОЙ, до всех проверок статуса: заглушку отсекает её природа, а не
    # состояние. Разбор — в :func:`_assert_not_a_stub`.
    _assert_not_a_stub(account)
    if account.status == "needs_reauth":
        raise ApiError(
            "unprocessable",
            status=422,
            message="Аккаунт требует переподключения через OAuth",
            details={"reason": "needs_reauth"},
        )
    if account.status == "disabled":
        expires_at = accounts_service.ensure_aware(account.token_expires_at)
        tokens_alive = expires_at is not None and expires_at > accounts_service.utcnow()
        if not tokens_alive:
            # access истёк за время простоя — пробуем refresh; отзыв -> needs_reauth
            outcome = await _refresh_outcome(account, db, redis)
            if outcome != accounts_service.REFRESH_OK:
                _raise_refresh_failure(account, outcome)
        account.status = "active"
        await write_audit(
            db,
            user_id=user.id,
            action="account.enabled",
            entity="avito_account",
            entity_id=str(account.id),
        )
        await db.commit()
        await accounts_service.register_webhook(account, redis)  # обратно к приёму (01 §4.5)
    return await _account_out(
        db, account, redis, operators=await _operators_summary(db, account.id)
    )
