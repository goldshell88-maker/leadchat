"""Ключи приложения Авито — из интерфейса, а не из файла на сервере.

ЗАЧЕМ. Подключение канала идёт через OAuth Авито, а для него нужны `client_id`
и `client_secret` приложения из кабинета разработчика. До сих пор они жили
только в `.env` на сервере: чтобы подключить первый боевой аккаунт, владельцу
пришлось бы звать разработчика с доступом по ssh. Заказчик сказал прямо: «я не
могу ввести данные для добавления аккаунта» и «сделай так, чтобы всё можно было
менять и добавлять без захода в код сервера».

ЧТО ЗДЕСЬ ХРАНИТСЯ. Четыре значения: пара ключей приложения и два адреса —
API и страницы согласия. Адреса нужны не для красоты: пока боевых ключей нет,
система работает на встроенном имитаторе, и переключение между ним и настоящим
Авито — это ровно смена этих двух адресов. Один выключатель в интерфейсе
вместо четырёх строк в файле, до которого не дотянуться.

СЕКРЕТ ХРАНИТСЯ ЗАШИФРОВАННЫМ и НИКОГДА не возвращается в браузер. Наружу
уходит только «задан» или «не задан»: показать секрет один раз — значит
показать его каждому, кто откроет этот экран через плечо. Менять его можно,
читать нельзя — как и пароль пользователя.

ПОЧЕМУ ЗНАЧЕНИЯ ИЗ ФАЙЛА ОСТАЮТСЯ ЗАПАСНЫМИ. Пока строки в базе нет, действует
`.env` — тот же приём, что у остальных настроек (`app_settings`). Это не
костыль совместимости: развёртывание задаёт стартовое состояние, владелец
переопределяет его из интерфейса, а сброс возвращает к тому, что задал инженер.

ПРО СВЕЖЕСТЬ. Клиент Авито создаётся в восьми местах, и половина из них — в
фоновых задачах без доступа к базе. Поэтому конфигурация живёт в кэше процесса,
а обновляется явно — вызовом ``ensure_fresh(db)`` там, где сессия есть и прямо
перед обращением к Авито. Кэш живёт полминуты: смена ключей доезжает до всех
процессов за это время, а обращения к базе не случаются на каждый запрос к
Авито.
"""

import base64
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.models import AppSetting
from app.services import crypto

log = structlog.get_logger(__name__)

#: Ключ строки в `app_settings`.
KEY = "avito.app"

#: Сколько кэш считается свежим. Полминуты — компромисс: смена ключей доезжает
#: до фоновых процессов за время, которое человек воспримет как «сразу», а
#: обращения к базе не случаются на каждый запрос к Авито.
CACHE_TTL_SECONDS = 30

#: Боевые адреса Авито. Вынесены сюда, чтобы переключатель «имитатор / боевой»
#: в интерфейсе не требовал от человека помнить или где-то искать эти строки.
LIVE_API_BASE = "https://api.avito.ru"
#: С `www` — ровно так, как отдаёт сам Авито. Заказчик прислал настоящую
#: ссылку своей прежней системы, и она идёт на www.avito.ru. Без него
#: Авито отвечает редиректом, а на редиректе параметры согласия (state,
#: scope) теряются не всегда, но иногда — и разбирать такое «иногда»
#: пришлось бы в день подключения боевого аккаунта.
LIVE_AUTH_URL = "https://www.avito.ru/oauth"


@dataclass(frozen=True)
class AvitoAppConfig:
    client_id: str
    client_secret: str
    api_base: str
    auth_url: str
    #: Откуда взялись значения — для честного ответа экрану настроек.
    source: str = "env"

    @property
    def is_live(self) -> bool:
        """Смотрим ли мы на настоящий Авито или на встроенный имитатор."""
        return self.api_base.rstrip("/") == LIVE_API_BASE


def _from_env() -> AvitoAppConfig:
    return AvitoAppConfig(
        client_id=settings.avito_client_id,
        client_secret=settings.avito_client_secret,
        api_base=settings.avito_api_base,
        auth_url=settings.avito_auth_url,
        source="env",
    )


# Кэш процесса: (когда обновлён, что лежит). Пустой — ещё ни разу не читали.
_cache: tuple[float, AvitoAppConfig] | None = None


def current() -> AvitoAppConfig:
    """Конфигурация для немедленного использования, без обращения к базе.

    Синхронно и намеренно: клиент Авито создаётся в конструкторах и в фоновых
    задачах, где сессии базы нет. Свежесть обеспечивает ``ensure_fresh``,
    который вызывают там, где сессия есть.
    """
    if _cache is not None:
        return _cache[1]
    return _from_env()


async def ensure_fresh(db: AsyncSession) -> AvitoAppConfig:
    """Обновить кэш, если он устарел. Зовётся ПЕРЕД обращением к Авито."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    config = await load(db)
    _cache = (now, config)
    return config


async def seed_process(session_factory: Any, component: str) -> None:
    """Прочитать настройки Авито из базы ОДИН РАЗ при старте процесса.

    ЗАЧЕМ. Адрес Авито — «настоящий» или встроенный имитатор — владелец меняет
    из интерфейса, значение живёт в базе, а клиент читает кэш процесса. У
    ТОЛЬКО ЧТО ПОДНЯВШЕГОСЯ процесса кэш пуст, и он берёт значение из `.env`:
    то, которое задал инженер при развёртывании, а не то, которое задал
    владелец. Пока первый запрос с сессией не обновит кэш, процесс работает по
    устаревшей настройке.

    Это уже стоило дня работы: после переключения на боевой Авито система
    продолжала ходить в имитатор, и владелец не мог привязать ни одного
    аккаунта. Места с сессией закрыты явно (`AvitoClient.fresh`), но остаются
    места без неё вовсе — отписка от вебхука, фоновая сверка, догрузка имени
    клиента. Один запрос к базе на старте закрывает их все разом.

    Падать из-за этого нельзя: не прочиталось — работаем по `.env`, как раньше.
    Не прочитаться оно может ровно в одном случае — база ещё не поднялась, — и
    тогда важнее запуститься, чем быть свежим.
    """
    try:
        async with session_factory() as db:
            config = await ensure_fresh(db)
        log.info(
            "avito_app.seeded",
            component=component,
            api_base=config.api_base,
            live=config.is_live,
        )
    except Exception:  # noqa: BLE001 — старт важнее свежести настройки
        log.warning("avito_app.seed_failed", component=component, exc_info=True)


def _drop_cache() -> None:
    """Сбросить кэш процесса — после сохранения новых ключей."""
    global _cache
    _cache = None


async def load(db: AsyncSession) -> AvitoAppConfig:
    """Прочитать конфигурацию из базы; чего нет — берём из окружения."""
    row = await db.get(AppSetting, KEY)
    if row is None or not isinstance(row.value, dict):
        return _from_env()

    stored: dict[str, Any] = row.value
    fallback = _from_env()

    secret = fallback.client_secret
    raw = stored.get("secret_enc")
    if isinstance(raw, str) and raw:
        try:
            secret = crypto.decrypt_token(base64.b64decode(raw))
        except (crypto.DecryptError, ValueError):
            # Ключ шифрования сменился (восстановление из копии на новом
            # сервере) — секрет нечитаем. Молчать нельзя: подключение будет
            # падать «неизвестно почему», а причина именно здесь.
            log.warning("avito_app.secret_unreadable")
            secret = ""

    def text(field: str, default: str) -> str:
        value = stored.get(field)
        return value if isinstance(value, str) and value else default

    return AvitoAppConfig(
        client_id=text("client_id", fallback.client_id),
        client_secret=secret,
        api_base=text("api_base", fallback.api_base),
        auth_url=text("auth_url", fallback.auth_url),
        source="db",
    )


async def save(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    client_id: str,
    client_secret: str | None,
    api_base: str,
    auth_url: str,
) -> AvitoAppConfig:
    """Сохранить ключи. ``client_secret=None`` — «оставить прежний».

    Отдельное значение для «не меняли» обязательно: экран не показывает
    секрет, и без этого различия каждое сохранение любой другой настройки
    стирало бы его пустой строкой.
    """
    client_id = client_id.strip()
    api_base = api_base.strip().rstrip("/")
    auth_url = auth_url.strip()

    if not client_id:
        raise _field_error("client_id", "Укажите Client ID приложения Авито")
    for field, addr in (("api_base", api_base), ("auth_url", auth_url)):
        if not addr.startswith(("http://", "https://")):
            raise _field_error(field, "Адрес должен начинаться с http:// или https://")

    current_config = await load(db)
    secret = current_config.client_secret if client_secret is None else client_secret.strip()
    if not secret:
        raise _field_error("client_secret", "Укажите Client Secret приложения Авито")

    value: dict[str, str] = {
        "client_id": client_id,
        "secret_enc": base64.b64encode(crypto.encrypt_token(secret)).decode(),
        "api_base": api_base,
        "auth_url": auth_url,
    }

    row = await db.get(AppSetting, KEY)
    if row is None:
        db.add(AppSetting(key=KEY, value=value, updated_by_id=actor_id))
    else:
        row.value = value
        row.updated_by_id = actor_id

    _drop_cache()
    log.info("avito_app.saved", live=api_base.rstrip("/") == LIVE_API_BASE)
    return replace(
        current_config,
        client_id=client_id,
        client_secret=secret,
        api_base=api_base,
        auth_url=auth_url,
        source="db",
    )


async def set_live(db: AsyncSession, *, actor_id: uuid.UUID | None, live: bool) -> AvitoAppConfig:
    """Переключить, КУДА система ходит: настоящий Авито или встроенный имитатор.

    ПОЧЕМУ ОТДЕЛЬНО ОТ :func:`save`. Сохранение требует пары ключей приложения —
    и это правильно для пути через согласие. Но каналы подключаются СВОИМИ
    ключами (решение заказчика от 8 августа), и ключей приложения на этой
    системе может не быть вовсе. Получалось, что переключить адрес нельзя, не
    придумав ключи, которых нет: интерфейс подставлял в них строку "unused" —
    то есть врал в поле, которое потом кто-нибудь прочитает как настоящее.

    Здесь меняются РОВНО два адреса. Ключи, если они заданы, остаются как были.

    Именно на это заказчик и наткнулся дважды: «я могу подключить только 1
    аккаунт» и «всё присваивается к фейк-авито». Имитатор на любые ключи
    отдавал один и тот же выдуманный аккаунт, а выйти из него было нечем.
    """
    current_config = await load(db)
    api_base = LIVE_API_BASE if live else settings.avito_api_base.rstrip("/")
    auth_url = LIVE_AUTH_URL if live else settings.avito_auth_url

    row = await db.get(AppSetting, KEY)
    value = dict(row.value) if row is not None else {}
    if not value:
        # Настроек в базе ещё нет: ключи, если они есть, приезжают из `.env` —
        # переносим их как есть, чтобы переключение адреса ничего не потеряло.
        value = {"client_id": current_config.client_id}
        if current_config.client_secret:
            value["secret_enc"] = base64.b64encode(
                crypto.encrypt_token(current_config.client_secret)
            ).decode()
    value["api_base"] = api_base
    value["auth_url"] = auth_url

    if row is None:
        db.add(AppSetting(key=KEY, value=value, updated_by_id=actor_id))
    else:
        row.value = value
        row.updated_by_id = actor_id

    _drop_cache()
    log.info("avito_app.mode", live=live, api_base=api_base)
    return replace(current_config, api_base=api_base, auth_url=auth_url, source="db")


async def reset(db: AsyncSession) -> AvitoAppConfig:
    """Вернуться к тому, что задано на сервере в `.env`."""
    await db.execute(sa.delete(AppSetting).where(AppSetting.key == KEY))
    _drop_cache()
    log.info("avito_app.reset")
    return _from_env()


def _field_error(field: str, message: str) -> ApiError:
    return ApiError(
        "validation_error",
        message,
        status=400,
        details={"fields": [{"field": field, "rule": "required", "message": message}]},
    )


def redirect_uri() -> str:
    """Адрес возврата, который нужно вписать в кабинете Авито.

    Показывается на экране настроек не для справки: если он не совпадёт с тем,
    что зарегистрировано у Авито, подключение будет падать без внятной
    причины — и искать расхождение человек будет часами.
    """
    return settings.avito_redirect
