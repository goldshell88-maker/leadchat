"""Песочница сценария — 02 §5.3, 01 §8.6, экран 11 §5.4.

Админ проверяет **черновик** сценария до сохранения: реальному клиенту ничего
не уходит, в `conversations`/`messages` не остаётся ни строки, состояние сессии
живёт в Redis (`sandbox:{admin_id}:{session_id}`, TTL 1 час).

**Движок — тот же, что в проде.** Здесь нет ни одной строки логики шагов:
модуль собирает `ScenarioEngine` (02 §2.3) поверх временного окружения,
пускает бота в диалог той же :func:`~app.bots.runtime.bot_entry_block`, что и
воркер входящих, и показывает трассировку самого движка (`Outbox.trace`).

Расхождения с продом ровно два, оба намеренные и оба перечислены ниже (пункты
2 и 3): своей логики шагов или своих правил входа у песочницы нет. Раньше
такие правила были — она проверяла только расписание и только на первом
сообщении, и после передачи менеджеру продолжала крутить сценарий, которого в
проде уже не бывает.

Как достигается «ничего не пишется» при живом движке, который работает с
сессией БД:

1. Тик выполняется в транзакции запроса, которая в `finally` **всегда**
   откатывается (:func:`_tick`). Ни одна из служебных строк (клиент, аккаунт,
   бот, диалог, история) не переживает запрос.
2. Внутри тика включён `no_autoflush`: сообщения, которые бот отправляет
   сейчас, остаются в сессии и до БД не доходят вовсе. Побочный эффект,
   осознанный: `ai_answer` в пределах одного тика видит историю без
   сообщений этого же тика — на следующем тике она восстановится из Redis.
3. Служебные строки истории пишутся с РЕАЛЬНЫМ временем, а не с
   `now_override`: `messages` партиционирована по месяцам (08 §6), и «сейчас»
   из будущего попало бы в несуществующую партицию.

`ai_mode`: `stub` — детерминированные заглушки (`confidence 0.9`, негатив по
слову «ужасно») для отладки графа офлайн; `real` — живые вызовы моделей через
`app.bots.ai` (за границу, через шлюз Амстердама; телефоны маскирует сам
движок — 02 §3.5).
"""

from __future__ import annotations

import importlib
import json
import secrets
import uuid
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.engine import ScenarioEngine
from app.bots.runtime import ENTRY_BLOCKS, bot_entry_block
from app.bots.state import BotState, Outbox
from app.models import AvitoAccount, Bot, Client, Conversation, Message

log = structlog.get_logger("app.bots.sandbox")

SESSION_TTL_SECONDS = 3600  # 02 §5.3: сессия песочницы живёт час
SESSION_KEY_PREFIX = "sandbox"
MAX_DIALOG_MESSAGES = 100  # предохранитель против бесконечной сессии в Redis

# Заглушки AI (02 §5.3): детерминированные, чтобы граф отлаживался офлайн.
STUB_REPLY = "[AI-ответ по базе знаний]"
STUB_CONFIDENCE = 0.9
STUB_NEGATIVE_MARKERS = ("ужасн", "отвратительн", "кошмар", "хамств")
STUB_HUMAN_MARKERS = ("оператор", "менеджер", "живой человек", "позовите человека")


class SandboxError(RuntimeError):
    """Базовая ошибка песочницы; роутер переводит её в ответ API."""


class SessionNotFound(SandboxError):
    """Сессия истекла по TTL или её никогда не было."""


class AIUnavailable(SandboxError):
    """`ai_mode="real"`, а модуль `app.bots.ai` недоступен (01 §8.6 → 503)."""


# --- заглушка AI -------------------------------------------------------------


class StubAI:
    """Реализация `AIBackend` движка (02 §3) без единого сетевого вызова."""

    async def ai_answer(
        self,
        bot: Any,
        dialog: list[dict[str, Any]],
        item_title: str | None,
        *,
        client_name: str = "",
        city: str = "",
        channel: dict[str, str] | None = None,
        conv_key: str = "",
    ) -> dict[str, Any]:
        return {
            "reply": STUB_REPLY,
            "confidence": STUB_CONFIDENCE,
            "needs_operator": False,
        }

    async def classify_message(self, texts: list[str]) -> dict[str, Any]:
        joined = " ".join(texts).lower()
        negative = any(marker in joined for marker in STUB_NEGATIVE_MARKERS)
        return {
            "sentiment": "negative" if negative else "neutral",
            "wants_human": any(marker in joined for marker in STUB_HUMAN_MARKERS),
            "reason": "заглушка песочницы",
        }

    # `extract_entities` намеренно нет: эшелон 2 (02 §3.5) — деньги за токены,
    # а сводку менеджеру он в песочнице не улучшает.


def ai_backend(ai_mode: str) -> Any:
    if ai_mode == "stub":
        return StubAI()
    try:
        module = importlib.import_module("app.bots.ai")
    except ModuleNotFoundError as exc:  # модуля нет (урезанная сборка)
        raise AIUnavailable("модуль app.bots.ai недоступен") from exc
    # Ключа нет и AI_FAKE выключен: звать некого. Честный 503 (01 §8.6) лучше,
    # чем сессия, где каждый шаг ai_answer молча уходит в handoff.
    if not module.is_available():
        raise AIUnavailable("ключа Anthropic на шлюзе нет")
    return module


# --- сессия ------------------------------------------------------------------


@dataclass
class SandboxSession:
    """Состояние сессии песочницы — ровно то, что лежит в Redis."""

    session_id: str
    admin_id: str
    scenario: dict[str, Any]
    knowledge_base: str = ""
    schedule: dict[str, Any] = field(default_factory=lambda: {"always": True})
    client_name: str | None = None
    item_title: str | None = None
    now_override: str | None = None
    ai_mode: str = "stub"
    # --- состояние «диалога» ---
    bot_active: bool = False
    bot_vars: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    tags: list[str] = field(default_factory=list)
    client_phone: str | None = None
    dialog: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> SandboxSession:
        """Сессия из Redis. Лишние ключи ОТБРАСЫВАЕМ, а не падаем на них.

        Сессия живёт час и переживает выкладку: `cls(**json.loads(...))` на
        записи от прошлой версии (поле переименовали или убрали) отвечал бы
        500 посреди отладки сценария. Проще начать заново, чем разбираться.
        """
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            log.info("sandbox.session_unknown_fields", fields=unknown)
        return cls(**{k: v for k, v in data.items() if k in known})

    def now(self) -> datetime:
        """«Сейчас» песочницы — влияет и на `work_hours`, и на расписание."""
        if self.now_override:
            moment = datetime.fromisoformat(self.now_override)
            return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return datetime.now(UTC)

    def state(self) -> dict[str, Any]:
        """Панель состояния справа (02 §5.3, 11 §5.4)."""
        bv = self.bot_vars or {}
        return {
            "step": bv.get("step"),
            "waiting": bv.get("waiting"),
            "vars": bv.get("vars", {}),
            "counters": bv.get("counters", {}),
            "bot_active": self.bot_active,
            "status": self.status,
            "tags": list(self.tags),
            "handoff": bv.get("handoff"),
        }

    def append_message(self, direction: str, sender_type: str, body: str) -> None:
        self.dialog.append({"direction": direction, "sender_type": sender_type, "body": body})
        del self.dialog[:-MAX_DIALOG_MESSAGES]


def session_key(admin_id: str, session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}:{admin_id}:{session_id}"


async def save_session(redis: Redis, session: SandboxSession) -> None:
    # TTL продлевается на каждом шаге: пока админ тестирует, сессия живая.
    await redis.set(
        session_key(session.admin_id, session.session_id),
        session.to_json(),
        ex=SESSION_TTL_SECONDS,
    )


async def load_session(redis: Redis, admin_id: str, session_id: str) -> SandboxSession:
    raw = await redis.get(session_key(admin_id, session_id))
    if raw is None:
        raise SessionNotFound(session_id)
    return SandboxSession.from_json(raw)


async def delete_session(redis: Redis, admin_id: str, session_id: str) -> bool:
    return bool(await redis.delete(session_key(admin_id, session_id)))


# --- временное окружение тика ------------------------------------------------


@dataclass
class _Scene:
    bot: Bot
    conv: Conversation
    client: Client


async def _seed(db: AsyncSession, session: SandboxSession) -> _Scene:
    """Служебные строки тика: клиент, аккаунт, бот, диалог и его история.

    Всё это откатывается в конце тика. `flush` нужен, потому что движок ходит
    в БД (`db.get(Client)`, `select(Message)` для контекста AI) — без реальных
    строк подстановка `{client_name}` и история для `ai_answer` не работали бы,
    и песочница врала бы про поведение прода.
    """
    account = AvitoAccount(
        id=uuid.uuid4(),
        title="Песочница",
        # avito_user_id UNIQUE: случайный, строка всё равно живёт до rollback
        avito_user_id=secrets.randbelow(9_000_000_000_000) + 1_000_000_000_000,
        access_token_enc=b"sandbox",
        refresh_token_enc=b"sandbox",
        token_expires_at=datetime.now(UTC) + timedelta(days=1),
        status="active",
        webhook_secret="sandbox",
    )
    client = Client(
        id=uuid.uuid4(),
        channel="avito",
        external_id=f"sandbox-{uuid.uuid4().hex[:12]}",
        name=session.client_name,
        phone=session.client_phone,
    )
    bot = Bot(
        id=uuid.uuid4(),
        name="Черновик в песочнице",
        is_enabled=True,
        schedule=session.schedule,
        scenario=session.scenario,
        knowledge_base=session.knowledge_base or None,
        # Песочница показывает, ЧТО получил бы клиент, — это про содержание сценария,
        # а не про способ доставки. Поэтому здесь всегда автоответ, даже если боевой бот
        # работает подсказкой: иначе трассировка показывала бы заметки вместо реплик.
        mode="auto",
    )
    db.add_all([account, client, bot])
    await db.flush()

    account.bot_id = bot.id
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=f"sandbox-{uuid.uuid4().hex[:12]}",
        account_id=account.id,
        client_id=client.id,
        status=session.status,
        bot_active=session.bot_active,
        bot_vars=json.loads(json.dumps(session.bot_vars or {})),
        tags=list(session.tags),
        item_title=session.item_title,
        unread_count=0,
    )
    db.add(conv)
    await db.flush()

    # История прошлых тиков — с РЕАЛЬНЫМ временем (партиции, см. docstring).
    base = datetime.now(UTC) - timedelta(seconds=len(session.dialog) + 1)
    for index, msg in enumerate(session.dialog):
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction=msg["direction"],
                sender_type=msg["sender_type"],
                body=msg["body"],
                attachments=[],
                delivery_status="delivered",
                created_at=base + timedelta(seconds=index),
            )
        )
    await db.flush()
    return _Scene(bot=bot, conv=conv, client=client)


def _absorb(session: SandboxSession, scene: _Scene, state: BotState) -> None:
    session.bot_vars = state.dump()
    session.bot_active = bool(scene.conv.bot_active)
    session.status = scene.conv.status
    session.tags = list(scene.conv.tags or [])
    session.client_phone = scene.client.phone


def _absorb_dialog(session: SandboxSession, trace: list[dict[str, Any]]) -> None:
    """Сообщения этого тика — в историю сессии (следующий тик их воспроизведёт)."""
    for note in trace:
        text = note.get("text")
        if not isinstance(text, str) or not text:
            continue
        if note.get("kind") == "bot_message":
            session.append_message("out", "bot", text)
        elif note.get("kind") == "note":
            session.append_message("note", "bot", text)


def _reply(session: SandboxSession, trace: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "events": trace,
        "state": session.state(),
        "trace": [
            n["id"] for n in trace if n.get("kind") == "step" and isinstance(n.get("id"), str)
        ],
    }


async def _tick(
    db: AsyncSession, session: SandboxSession, incoming: str | None
) -> list[dict[str, Any]]:
    """Один вызов движка — ровно то же, что делает `bot_step` в проде (02 §2.3)."""
    outbox = Outbox()
    # Порядок как в проде (08 §3): воркер входящих СНАЧАЛА пишет сообщение
    # клиента, и только потом зовёт `bot_step`. Иначе классификатор негатива
    # и `ai_answer` не увидели бы реплику, на которую отвечают.
    if incoming is not None:
        session.append_message("in", "client", incoming)
        # Клиент вернулся в закрытый диалог — его переоткрывает воркер входящих
        # ДО проверки входа бота (app/services/inbound.py, DESIGN §8.3). Без
        # этой строки песочница отвечала бы «диалог не в статусе Новый» там,
        # где прод запускает сценарий с чистого листа.
        if session.status == "closed":
            session.status = "new"
    try:
        scene = await _seed(db, session)
        # ВОРОТА ВХОДА — ТА ЖЕ ФУНКЦИЯ, ЧТО В ПРОДЕ.
        #
        # Раньше песочница проверяла только расписание и только на первом
        # сообщении сессии. Отсюда два вранья, которые админ принимал за
        # правду: после передачи менеджеру бот продолжал крутить сценарий и
        # писал «↯ мимо сценария 1, 2, 3» (в проде `handoff` закрывает вход
        # навсегда), а после шага `close` перезапускался даже вне расписания.
        # Проверять нечего только на таймауте: `bot_ask_timeout` в проде тоже
        # смотрит лишь на `bot_active` и токен ожидания (02 §2.4).
        block = (
            await bot_entry_block(db, scene.conv, bot=scene.bot, now=session.now())
            if incoming is not None
            else None
        )
        if block is not None:
            # Подпись отдаём готовой: у редактора своего словаря причин нет и
            # разойтись с сервером ему нечем.
            outbox.note("skipped", reason=block, label=ENTRY_BLOCKS.get(block, block))
        else:
            state = BotState.from_conv(scene.conv)
            engine = ScenarioEngine(
                bot=scene.bot,
                conv=scene.conv,
                state=state,
                db=db,
                outbox=outbox,
                ai=ai_backend(session.ai_mode),
                now=session.now,
                scenario=session.scenario,
            )
            with db.no_autoflush:  # исходящие этого тика до БД не доходят
                if incoming is not None:
                    await engine.on_incoming(incoming)
                else:
                    await engine.on_timeout()
                await engine.run()
                # В проде классификатор негатива работает отдельной короткой
                # транзакцией после commit'а (02 §3.4); в песочнице ответ должен
                # быть синхронным — иначе «ужасный сервис!» нечем проверить.
                if engine.pending_classification:
                    await engine.classify_and_react(engine.pending_classification)
            _absorb(session, scene, state)
    finally:
        # Единственная гарантия «в БД ничего не остаётся» — не соглашение,
        # а откат: даже упавший тик не оставляет строк.
        await db.rollback()
        db.expunge_all()
    _absorb_dialog(session, outbox.trace)
    return outbox.trace


# --- публичный API песочницы -------------------------------------------------


async def start(
    redis: Redis,
    admin_id: str,
    *,
    scenario: dict[str, Any],
    knowledge_base: str = "",
    schedule: dict[str, Any] | None = None,
    client_name: str | None = None,
    item_title: str | None = None,
    now_override: str | None = None,
    ai_mode: str = "stub",
) -> tuple[SandboxSession, dict[str, Any]]:
    """Открыть сессию. Первый тик пуст: бот ждёт первого сообщения клиента."""
    session = SandboxSession(
        session_id=secrets.token_urlsafe(12),
        admin_id=admin_id,
        scenario=scenario,
        knowledge_base=knowledge_base or "",
        schedule=schedule or {"always": True},
        client_name=client_name,
        item_title=item_title,
        now_override=now_override,
        ai_mode=ai_mode,
        created_at=datetime.now(UTC).isoformat(),
    )
    if ai_mode == "real":
        ai_backend(ai_mode)  # ранний 503 вместо падения на первом сообщении
    await save_session(redis, session)
    log.info("sandbox.started", session_id=session.session_id, ai_mode=ai_mode)
    return session, {"events": [], "state": session.state(), "trace": []}


async def message(
    redis: Redis, db: AsyncSession, admin_id: str, session_id: str, text: str
) -> dict[str, Any]:
    """Сообщение «клиента» — тот же путь, что `bot_step(conv_id, text)` в проде.

    Пускать ли бота, решает `bot_entry_block` внутри тика — та же функция, что
    в воркере входящих; своей проверки у песочницы больше нет.
    """
    session = await load_session(redis, admin_id, session_id)
    trace = await _tick(db, session, text)
    await save_session(redis, session)
    return _reply(session, trace)


async def fire_timeout(
    redis: Redis, db: AsyncSession, admin_id: str, session_id: str
) -> dict[str, Any]:
    """«⏩ Промотать таймаут» — эмуляция `bot_ask_timeout` без ожидания 24 часов."""
    session = await load_session(redis, admin_id, session_id)
    if not (session.bot_vars or {}).get("waiting"):
        return {
            "events": [{"kind": "skipped", "reason": "not_waiting"}],
            "state": session.state(),
            "trace": [],
        }
    trace = await _tick(db, session, None)
    await save_session(redis, session)
    return _reply(session, trace)


async def stop(redis: Redis, admin_id: str, session_id: str) -> None:
    if not await delete_session(redis, admin_id, session_id):
        raise SessionNotFound(session_id)
