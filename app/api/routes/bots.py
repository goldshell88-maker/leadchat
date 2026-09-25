"""Боты: CRUD сценариев и песочница — 01 §8, экраны 11 §5.

Право на весь модуль одно — `bots:manage`, и оно есть только у admin
(01 §12, DESIGN §5.1). Никаких «head тоже посмотрит»: сценарий бота — это
код, который говорит с клиентами от имени сервиса.

Три решения, которые видно прямо в наборе ручек:

* **PATCH у ботов нет.** Сценарий — атомарный документ: частичное обновление
  графа шагов не имеет смысла и порождает нецелостные состояния (01 §8.3).
  Включение/выключение — отдельные ручки `enable`/`disable`, привязка
  аккаунтов — отдельный `PUT .../accounts`.
* **Валидация — двухслойная и серверная** (02 §1.4 + §5.2). Клиент валидирует
  для мгновенного фидбека, но истина здесь: `422 bot_scenario_invalid`
  со списком `{step_id, field, code, message}`.
* **Удаление привязанного бота — `409`, а не каскад** (01 §8.7): отвязка
  аккаунтов должна быть осознанным действием админа.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.bots import sandbox as sandbox_mod
from app.bots.runtime import flush_outbox, release_bot_dialogs
from app.bots.scenarios import DEFAULT_KNOWLEDGE_BASE, default_scenario
from app.bots.validator import (
    first_error_message,
    has_errors,
    issues_details,
    validate_scenario,
)
from app.core.errors import ApiError
from app.models import AvitoAccount, Bot, Conversation, User
from app.schemas.bots import (
    BotAccountOut,
    BotAccountsOut,
    BotAccountsWrite,
    BotDetailOut,
    BotListItemOut,
    BotsPageOut,
    BotWrite,
    PageOut,
    SandboxMessageIn,
    SandboxStartIn,
    SandboxStartOut,
    SandboxTickOut,
)
from app.services.audit import write_audit

router = APIRouter()
log = structlog.get_logger("app.bots.api")

manage = require_permission("bots:manage")  # 01 §12: только admin


# --- вспомогательное ---------------------------------------------------------


async def _accounts_of(db: AsyncSession, bot_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    """`avito_accounts.bot_id` — единственная истина о привязке (01 §8.1)."""
    if not bot_ids:
        return {}
    rows = (
        await db.execute(
            select(AvitoAccount.id, AvitoAccount.title, AvitoAccount.bot_id)
            .where(AvitoAccount.bot_id.in_(bot_ids))
            .order_by(AvitoAccount.title)
        )
    ).all()
    grouped: dict[uuid.UUID, list[Any]] = {bot_id: [] for bot_id in bot_ids}
    for account_id, title, bot_id in rows:
        grouped[bot_id].append(BotAccountOut(id=account_id, title=title))
    return grouped


BOT_ACTIVITY_WINDOW_DAYS = 7


async def _conversations_7d(db: AsyncSession, bot_ids: list[uuid.UUID]) -> dict[str, int]:
    """Колонка «Диалогов/7д» списка ботов (11 §5.1).

    Считаем по `bot_vars.bot_id` — это единственный след бота в диалоге,
    который переживает и handoff, и закрытие (привязка `avito_accounts.bot_id`
    не годится: аккаунт мог поменять бота вчера, а диалоги за неделю вёл
    предыдущий).
    """
    if not bot_ids:
        return {}
    since = datetime.now(UTC) - timedelta(days=BOT_ACTIVITY_WINDOW_DAYS)
    marker = Conversation.bot_vars["bot_id"].as_string()
    wanted = [str(b) for b in bot_ids]
    rows = (
        await db.execute(
            select(marker, sa.func.count(sa.distinct(Conversation.id)))
            .where(marker.in_(wanted), Conversation.updated_at >= since)
            .group_by(marker)
        )
    ).all()
    counts = dict.fromkeys(wanted, 0)
    for key, count in rows:
        if key in counts:
            counts[key] = int(count)
    return counts


async def _get_bot_or_404(db: AsyncSession, bot_id: uuid.UUID) -> Bot:
    """Бот по идентификатору. Системная запись отсюда НЕ ВИДНА.

    ⚠ ЛИД-БОТ ЗДЕСЬ — «НЕ НАЙДЕН», И ЭТО НЕ ЛОЖЬ, А ГРАНИЦА. Решение владельца
    от 12 августа: «сам LeadBot это не совсем бот, я хочу чтобы у него была своя
    собственная вкладка и настройки». Внутри он остался записью в `bots` (в
    движке живут расписание, замолкание при менеджере, handoff и лимиты — см.
    `app/services/leadbot_admin.py`), но управляется он СВОИМ разделом.

    404, а не 403: 403 означал бы «есть, но не для вас» и подсказывал бы
    существование записи, которой в интерфейсе ботов нет. Открыть лид-бота
    редактором сценариев нельзя вовсе — там его сценарий из трёх шагов, и
    правка его руками сломала бы разговор, который ведёт чужая сторона.
    """
    bot = await db.get(Bot, bot_id)
    if bot is None or bot.is_system:
        raise ApiError("not_found", "Бот не найден", status=404)
    return bot


def _steps(scenario: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = (scenario or {}).get("steps")
    return steps if isinstance(steps, list) else []


async def _detail(db: AsyncSession, bot: Bot) -> BotDetailOut:
    accounts = (await _accounts_of(db, [bot.id])).get(bot.id, [])
    return BotDetailOut(
        id=bot.id,
        name=bot.name,
        is_enabled=bot.is_enabled,
        schedule=bot.schedule or {"always": True},
        scenario=bot.scenario or {},
        knowledge_base=bot.knowledge_base,
        ai_provider=getattr(bot, "ai_provider", "claude"),
        mode=getattr(bot, "mode", "suggest"),
        accounts=accounts,
    )


def _validate_or_422(scenario: Any, knowledge_base: str | None) -> list[Any]:
    """Слой 1 + слой 2 (02 §5.2). `error` блокирует сохранение, `warning` — нет."""
    issues = validate_scenario(scenario, knowledge_base=knowledge_base)
    if has_errors(issues):
        raise ApiError(
            "bot_scenario_invalid",
            first_error_message(issues),
            status=422,
            details=issues_details(issues),
        )
    return [i for i in issues if not i.is_error]


def _diff_steps(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Изменённые/добавленные/удалённые шаги — для `audit_log.details` (02 §5.2)."""
    before = {s.get("id"): s for s in _steps(old) if isinstance(s, dict)}
    after = {s.get("id"): s for s in _steps(new) if isinstance(s, dict)}
    changed = {sid for sid in before.keys() | after.keys() if before.get(sid) != after.get(sid)}
    return sorted(str(sid) for sid in changed if sid is not None)


# --- песочница (01 §8.6, 02 §5.3) -------------------------------------------
# Объявлена до `/bots/{bot_id}`: пути не пересекаются, но порядок делает
# намерение явным и защищает от будущего расширения путей.


def _sandbox_error(exc: sandbox_mod.SandboxError) -> ApiError:
    if isinstance(exc, sandbox_mod.SessionNotFound):
        return ApiError("not_found", "Сессия песочницы истекла — начните заново", status=404)
    # ai_mode="real" без доступной AI-подсистемы (01 §8.6)
    return ApiError("upstream_unavailable", f"AI недоступен: {exc}", status=503)


@router.post("/bots/sandbox/start", response_model=SandboxStartOut)
async def sandbox_start(
    body: SandboxStartIn,
    user: User = Depends(manage),
    redis: Redis = Depends(get_redis),
) -> SandboxStartOut:
    """Открыть сессию на ЧЕРНОВИКЕ сценария. В БД не пишется ничего."""
    _validate_or_422(body.scenario, body.knowledge_base)
    try:
        session, tick = await sandbox_mod.start(
            redis,
            str(user.id),
            scenario=body.scenario,
            knowledge_base=body.knowledge_base,
            schedule=body.schedule.model_dump(),
            client_name=body.client_name,
            item_title=body.item_title,
            now_override=body.now_override.isoformat() if body.now_override else None,
            ai_mode=body.ai_mode,
        )
    except sandbox_mod.SandboxError as exc:
        raise _sandbox_error(exc) from exc
    return SandboxStartOut(session_id=session.session_id, **tick)


@router.post("/bots/sandbox/{session_id}/message", response_model=SandboxTickOut)
async def sandbox_message(
    session_id: str,
    body: SandboxMessageIn,
    user: User = Depends(manage),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> SandboxTickOut:
    """Сообщение «клиента» — тот же путь, что `bot_step` в проде."""
    try:
        tick = await sandbox_mod.message(redis, db, str(user.id), session_id, body.text)
    except sandbox_mod.SandboxError as exc:
        raise _sandbox_error(exc) from exc
    return SandboxTickOut(**tick)


@router.post("/bots/sandbox/{session_id}/fire-timeout", response_model=SandboxTickOut)
async def sandbox_fire_timeout(
    session_id: str,
    user: User = Depends(manage),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> SandboxTickOut:
    """«⏩ Промотать таймаут» — эмуляция `bot_ask_timeout` без ожидания 24 часов."""
    try:
        tick = await sandbox_mod.fire_timeout(redis, db, str(user.id), session_id)
    except sandbox_mod.SandboxError as exc:
        raise _sandbox_error(exc) from exc
    return SandboxTickOut(**tick)


@router.delete("/bots/sandbox/{session_id}", status_code=204)
async def sandbox_stop(
    session_id: str,
    user: User = Depends(manage),
    redis: Redis = Depends(get_redis),
) -> Response:
    try:
        await sandbox_mod.stop(redis, str(user.id), session_id)
    except sandbox_mod.SandboxError as exc:
        raise _sandbox_error(exc) from exc
    return Response(status_code=204)


# --- CRUD --------------------------------------------------------------------


@router.get("/bots", response_model=BotsPageOut)
async def list_bots(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> BotsPageOut:
    # Системная запись лид-бота из списка исключена — и из счётчика тоже:
    # «ботов: 3» при двух видимых читается как потерянная строка.
    visible = Bot.is_system.is_(False)
    total = (await db.execute(select(sa.func.count()).select_from(Bot).where(visible))).scalar_one()
    bots = list(
        (
            await db.execute(
                select(Bot).where(visible).order_by(Bot.name, Bot.id).limit(limit).offset(offset)
            )
        ).scalars()
    )
    bot_ids = [b.id for b in bots]
    accounts = await _accounts_of(db, bot_ids)
    activity = await _conversations_7d(db, bot_ids)
    return BotsPageOut(
        items=[
            BotListItemOut(
                id=bot.id,
                name=bot.name,
                is_enabled=bot.is_enabled,
                schedule=bot.schedule or {"always": True},
                accounts=accounts.get(bot.id, []),
                scenario_steps_count=len(_steps(bot.scenario)),
                knowledge_base_present=bool((bot.knowledge_base or "").strip()),
                ai_provider=getattr(bot, "ai_provider", "claude"),
                mode=getattr(bot, "mode", "suggest"),
                conversations_7d=activity.get(str(bot.id), 0),
            )
            for bot in bots
        ],
        page=PageOut(limit=limit, offset=offset, total=total),
    )


@router.get("/bots/{bot_id}", response_model=BotDetailOut)
async def get_bot(
    bot_id: uuid.UUID,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> BotDetailOut:
    return await _detail(db, await _get_bot_or_404(db, bot_id))


@router.post("/bots", response_model=BotDetailOut, status_code=201)
async def create_bot(
    body: BotWrite,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> BotDetailOut:
    """Создать бота. Без `scenario` — копия дефолтного «Первичного приёма» (02 §5.1)."""
    scenario = body.scenario if body.scenario is not None else default_scenario()
    knowledge_base = body.knowledge_base or DEFAULT_KNOWLEDGE_BASE
    warnings = _validate_or_422(scenario, knowledge_base)
    scenario = {**scenario, "revision": 1}  # ревизией владеет сервер

    bot = Bot(
        id=uuid.uuid4(),
        name=body.name,
        is_enabled=body.is_enabled,
        schedule=body.schedule.model_dump(),
        scenario=scenario,
        knowledge_base=knowledge_base or None,
        ai_provider=body.ai_provider,
        mode=body.mode,
    )
    db.add(bot)
    await write_audit(
        db,
        user_id=user.id,
        action="bot.updated",
        entity="bot",
        entity_id=str(bot.id),
        details={"created": True, "revision": 1, "warnings": len(warnings)},
    )
    await db.commit()
    log.info("bot.created", bot_id=str(bot.id), steps=len(_steps(scenario)))
    return await _detail(db, bot)


@router.put("/bots/{bot_id}", response_model=BotDetailOut)
async def replace_bot(
    bot_id: uuid.UUID,
    body: BotWrite,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> BotDetailOut:
    """Полная замена бота вместе со сценарием (01 §8.3). PATCH намеренно нет."""
    if body.scenario is None:
        # Текст видит админ в тосте редактора, а не разработчик в логе: ни
        # «PUT», ни имени поля `scenario` он в интерфейсе не встречал (TEXT-31).
        raise ApiError(
            "validation_error",
            "Бот сохраняется вместе со сценарием — без него сохранить нельзя",
            status=400,
        )
    bot = await _get_bot_or_404(db, bot_id)
    knowledge_base = body.knowledge_base
    warnings = _validate_or_422(body.scenario, knowledge_base)

    old_scenario = bot.scenario or {}
    old_revision = old_scenario.get("revision")
    revision = (old_revision if isinstance(old_revision, int) else 0) + 1
    scenario = {**body.scenario, "revision": revision}
    diff = _diff_steps(old_scenario, scenario)

    # Смена мозга и смена режима — самое дорогое, что делается на этом экране: с этой
    # минуты клиенты начинают получать машинный текст (или перестают), причём текст
    # может быть написан по ЧУЖОМУ регламенту. В `diff_steps` ни того, ни другого не
    # видно, а разбирать потом «кто выпустил бота к людям» придётся именно по аудиту.
    old_mode = getattr(bot, "mode", "suggest")
    old_provider = getattr(bot, "ai_provider", "claude")
    # Автоответ → подсказка: подсказка диалог не ведёт, и диалоги, которые вёл
    # бот, уходят людям сразу (проверка 24.09). ДО смены режима: передача берёт
    # режим из базы, а после автоответа диалог встаёт в очередь с этой секунды.
    outbox = (
        await release_bot_dialogs(
            db, account_ids=await _bound_account_ids(db, bot.id), reason="bot_to_suggest"
        )
        if old_mode == "auto" and body.mode == "suggest"
        else None
    )

    bot.name = body.name
    # `is_enabled` здесь НЕ ЧИТАЕМ (проверка 24.09): включение — ручки `enable` и
    # `disable` (01 §8.4). Редактор, собравший черновик из кэша до выключения,
    # присылал `true`, и «Сохранить» включал бота, которого только что выключили.
    bot.schedule = body.schedule.model_dump()
    bot.scenario = scenario
    bot.knowledge_base = knowledge_base or None
    bot.ai_provider = body.ai_provider
    bot.mode = body.mode

    details: dict[str, Any] = {
        "revision": revision,
        "diff_steps": diff,
        "warnings": len(warnings),
    }
    if body.mode != old_mode:
        details["mode"] = {"from": old_mode, "to": body.mode}
    if body.ai_provider != old_provider:
        details["ai_provider"] = {"from": old_provider, "to": body.ai_provider}

    await write_audit(
        db,
        user_id=user.id,
        action="bot.updated",
        entity="bot",
        entity_id=str(bot.id),
        details=details,
    )
    await db.commit()
    if outbox is not None:
        await flush_outbox({"redis": redis}, outbox)
    log.info(
        "bot.updated",
        bot_id=str(bot.id),
        revision=revision,
        diff_steps=diff,
        ai_provider=body.ai_provider,
        mode=body.mode,
    )
    return await _detail(db, bot)


@router.post("/bots/{bot_id}/enable", response_model=BotDetailOut)
async def enable_bot(
    bot_id: uuid.UUID,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> BotDetailOut:
    return await _set_enabled(db, redis, bot_id, user, enabled=True)


@router.post("/bots/{bot_id}/disable", response_model=BotDetailOut)
async def disable_bot(
    bot_id: uuid.UUID,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> BotDetailOut:
    """Выключение мгновенно: воркер проверяет `is_enabled` перед каждым шагом
    (01 §8.4), а диалоги, которые бот вёл, сразу уходят людям (проверка 24.09):
    `bot_active` прячет диалог из очереди, и оставить его значило бы спрятать
    клиентов выключенного бота."""
    return await _set_enabled(db, redis, bot_id, user, enabled=False)


async def _set_enabled(
    db: AsyncSession, redis: Redis, bot_id: uuid.UUID, user: User, *, enabled: bool
) -> BotDetailOut:
    bot = await _get_bot_or_404(db, bot_id)
    if bot.is_enabled != enabled:
        bot.is_enabled = enabled
        await write_audit(
            db,
            user_id=user.id,
            action="bot.updated",
            entity="bot",
            entity_id=str(bot.id),
            details={"is_enabled": enabled},
        )
        outbox = (
            await release_bot_dialogs(db, account_ids=await _bound_account_ids(db, bot.id))
            if not enabled
            else None
        )
        await db.commit()
        if outbox is not None:
            await flush_outbox({"redis": redis}, outbox)
    return await _detail(db, bot)


async def _bound_account_ids(db: AsyncSession, bot_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        (await db.execute(select(AvitoAccount.id).where(AvitoAccount.bot_id == bot_id))).scalars()
    )


@router.put("/bots/{bot_id}/accounts", response_model=BotAccountsOut)
async def set_bot_accounts(
    bot_id: uuid.UUID,
    body: BotAccountsWrite,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> BotAccountsOut:
    """Привязка бота к аккаунтам (01 §8.5).

    Тело — полный список: аккаунты вне списка отвязываются. Аккаунт, занятый
    другим ботом, молча перепривязывается — у аккаунта ровно один бот.
    """
    bot = await _get_bot_or_404(db, bot_id)
    wanted = list(body.account_ids)
    rows = await db.execute(
        select(AvitoAccount.id, AvitoAccount.bot_id).where(
            sa.or_(AvitoAccount.bot_id == bot.id, AvitoAccount.id.in_(wanted))
        )
    )
    owners = dict(rows.tuples().all())
    # Где у канала сменился бот, диалоги старого уходят людям (проверка 24.09):
    # снятые с этого бота и перепривязанные от другого.
    unbound = {a for a, owner in owners.items() if owner == bot.id and a not in wanted}
    taken = {a for a, owner in owners.items() if a in wanted and owner not in (None, bot.id)}
    if wanted:
        missing = [str(a) for a in wanted if a not in owners]
        if missing:
            raise ApiError(
                "not_found",
                "Аккаунт Авито не найден",
                status=404,
                details={"account_ids": missing},
            )
        await db.execute(
            sa.update(AvitoAccount).where(AvitoAccount.id.in_(wanted)).values(bot_id=bot.id)
        )
    await db.execute(
        sa.update(AvitoAccount)
        .where(
            AvitoAccount.bot_id == bot.id, AvitoAccount.id.notin_(wanted) if wanted else sa.true()
        )
        .values(bot_id=None)
    )
    await write_audit(
        db,
        user_id=user.id,
        action="bot.updated",
        entity="bot",
        entity_id=str(bot.id),
        details={"accounts": [str(a) for a in wanted]},
    )
    outbox = await release_bot_dialogs(db, account_ids=unbound | taken)
    await db.commit()
    await flush_outbox({"redis": redis}, outbox)
    return BotAccountsOut(accounts=(await _accounts_of(db, [bot.id])).get(bot.id, []))


@router.delete("/bots/{bot_id}", status_code=204)
async def delete_bot(
    bot_id: uuid.UUID,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """`409`, если на бота ссылаются аккаунты (01 §8.7) — каскада нет намеренно."""
    bot = await _get_bot_or_404(db, bot_id)
    bound = (await _accounts_of(db, [bot.id])).get(bot.id, [])
    if bound:
        raise ApiError(
            "conflict",
            "Бот привязан к аккаунтам — сначала отвяжите их",
            status=409,
            details={
                "reason": "bot_in_use",
                "accounts": [{"id": str(a.id), "title": a.title} for a in bound],
            },
        )
    name = bot.name
    await db.delete(bot)
    await write_audit(
        db,
        user_id=user.id,
        # Своё событие, а не `bot.updated` с признаком в подробностях: удаление
        # сценария необратимо, а в журнале выглядело как обычное сохранение.
        action="bot.deleted",
        entity="bot",
        entity_id=str(bot_id),
        details={"name": name},
    )
    await db.commit()
    log.info("bot.deleted", bot_id=str(bot_id))
    return Response(status_code=204)
