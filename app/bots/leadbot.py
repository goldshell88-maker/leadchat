"""Лид-бот как поставщик ответов для шага `ai_answer` — вместо прямого вызова модели.

ЗАЧЕМ. Лид-бот — отдельный продукт владельца, живущий на своём сервере. Он отвечает
клиентам Авито по регламенту, который в LeadChat не переносили и переносить не собираются:
роутеры приёма и отказа, ценовая лестница, RAG на живых парах, гарды стиля, эскалация с
причиной и сроком. До сих пор он работал через Jivo с человеком в контуре. Владелец решил
подключить его к LeadChat автоответом, и вся подмена сводится к одному: на шаге `ai_answer`
спросить не Claude, а его.

ПОЧЕМУ ЭТО ВООБЩЕ ПОМЕСТИЛОСЬ В ОДИН ФАЙЛ. `ScenarioEngine` не знает, кто именно ему
отвечает: он зовёт `ai_answer(bot, dialog, item_title)` и разбирает `{reply, confidence,
needs_operator}` либо `None`. Здесь ровно этот протокол, только по HTTP. Замолкание при
менеджере, расписание, handoff, лимиты — всё это остаётся в движке и не трогается.

ГЛАВНОЕ ПРАВИЛО, КОТОРОЕ ЗДЕСЬ НЕЛЬЗЯ НАРУШИТЬ (решение владельца №4, DESIGN §4.5):
недоступность ИИ НИКОГДА не блокирует доставку сообщений. Поэтому наружу отсюда не
вылетает ни одного исключения, и нет ни одного повтора: таймаут, отказ сети, 500, 401,
не-JSON, JSON не той формы — всё это одинаковый `None`, а `None` движок штатно превращает
в handoff человеку. Повтор здесь стоил бы вдвое больше секунд внутри того же бюджета —
то есть увеличил бы шанс, что клиент не получит вообще ничего.

ЧТО НА ТОЙ СТОРОНЕ. Ручка уже есть: `POST /api/leadchat/answer`, токен в заголовке
`Authorization: Bearer` (`brain/server.py`, `brain/leadchat.py` в его репозитории). Полное
требование к ней — `docs/42-TZ-LEADBOT.md`; там же описано, что будет при отказе.

⚠ ТЕЛЕФОНЫ УХОДЯТ СЮДА СЫРЫМИ — И ЭТО ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ ВО ВСЁМ ПРОДУКТЕ.

Всякой другой модели `engine.load_dialog_for_ai` заменяет номера на `{PHONE}`: запрос к
Claude уходит за границу через прокси, и переписке клиентов там делать нечего. Лид-боту
диалог уходит как есть, без маски (`mask=False`, `engine._ai_request`).

ПОЧЕМУ. Его воронка ищет номер в репликах, чтобы НЕ просить его повторно. С маской бот
не видел уже данный телефон и переспрашивал — самый заметный признак робота; сам лид-бот
предупреждал об этом в `meta.warnings`. Цена размена названа честно: адресат — сервер
владельца в его же контуре (`LEADBOT_URL`, приватный адрес за WireGuard), запрос за
периметр не выходит.

КОГДА И ЧЬИМ РЕШЕНИЕМ. 16 августа 2026, коммит 906a6e8 «Лид-боту — сырые телефоны и
полная история». До него маска стояла и здесь. Держит поведение сторож
`tests/unit/test_bot_leadbot.py::test_лид_бот_видит_телефон_сырым`: он требует живой
номер в теле запроса и отсутствие `{PHONE}`.

ЧТО ЭТО НЕ ОТМЕНЯЕТ. В наш собственный журнал (`leadbot_calls.question`) телефон не
попадает: `engine` маскирует вопрос уже при записи. Снятие маски касается ровно одного
адресата — лид-бота. Разбор — в `docs/42-TZ-LEADBOT.md` §7.1.

⚠ СЕКРЕТ. Токен не попадает ни в `repr`, ни в журнал, ни в ответ API. Это не паранойя:
`repr` объекта уезжает в трассировку, трассировка — в Sentry, а токен открывает чужому
сервису всю переписку. Поэтому у конфигурации свой `__repr__`, а наружу отдаётся
`safe_dump()` — адрес и «задан/не задан».
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AppSetting
from app.services import crypto

log = structlog.get_logger("app.bots.leadbot")

#: Путь ручки на стороне лид-бота. Константа, а не настройка: это контракт двух
#: систем (docs/42), и разъехаться он может только вместе с обеими.
ANSWER_PATH = "/api/leadchat/answer"

#: Ключ строки в `app_settings` — как `avito.app` у ключей приложения Авито.
KEY = "leadbot"

#: Столько кэш процесса считается свежим. Полминуты — то же число и по той же
#: причине, что в `services/avito_app.py`: смена адреса доезжает до воркеров за
#: время, которое человек воспримет как «сразу», а к базе мы за этим не ходим
#: на каждый тик бота.
CACHE_TTL_SECONDS = 30

#: Верхний предел ответа. Шаг сценария режет ещё раз своим `max_reply_len`; этот
#: предел — про другое: не дать чужому сервису положить нам сообщение на 200 КБ
#: в базу и в Авито, у которого свой лимит.
MAX_REPLY_CHARS = 1000


# --------------------------------------------------------------- настройка


@dataclass
class LeadbotReply:
    """Что вернул один вызов лид-бота — целиком, включая причину отказа.

    ЗАЧЕМ НЕ ПРОСТО `dict | None`. `None` отвечает на вопрос движка («делать ли
    handoff») и молчит про всё остальное. Журналу работы и экрану проверки
    нужно ровно остальное: сколько ждали, что именно не получилось, какой слой
    ответил на той стороне, была ли эскалация. Раньше это уходило в поток логов
    и там оставалось — то есть было доступно одному человеку в проекте.
    """

    request_id: str
    ms: int
    #: HTTP-код. `None` — до ответа не дошло вовсе (таймаут, сеть).
    status: int | None = None
    #: Разобранный контракт шага. `None` — вызывающий обязан сделать handoff.
    answer: dict[str, Any] | None = None
    #: Весь ответ как есть — ради `meta`. `None`, если разбирать было нечего.
    raw: dict[str, Any] | None = None
    #: Причина отказа человеческими словами. Пусто — обращение удалось.
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.answer is not None

    @property
    def meta(self) -> dict[str, Any]:
        meta = (self.raw or {}).get("meta")
        return meta if isinstance(meta, dict) else {}


#: Последний вызов лид-бота в этой задаче — для журнала работы.
#:
#: ПОЧЕМУ КОНТЕКСТНАЯ ПЕРЕМЕННАЯ, А НЕ ВОЗВРАТ. Запись журнала собирается из
#: двух мест сразу: ЧТО ответил лид-бот знает только этот модуль, а ЧЕМ ЭТО
#: КОНЧИЛОСЬ ДЛЯ КЛИЕНТА — только движок (это он решает: отправить, положить
#: подсказкой, выбросить за низкую уверенность). Протаскивать `meta` через
#: контракт `ai_answer` нельзя: он общий с Claude, у которого этих полей не
#: бывает. А главное — при недоступности `ai_answer` возвращает `None`, и
#: возвращать в нём стало бы нечего ровно в том случае, ради которого журнал и
#: заводится.
#:
#: Приём в проекте не новый: так же передаётся `request_id` в `core/logging.py`.
last_call: ContextVar[LeadbotReply | None] = ContextVar("leadbot_last_call", default=None)


@dataclass(frozen=True)
class LeadbotConfig:
    """Адрес и токен лид-бота. Токен намеренно вне `repr` — см. шапку файла."""

    url: str
    token: str = field(repr=False, default="")
    #: Откуда взялись значения — для честного ответа экрану настроек.
    source: str = "env"

    @property
    def is_ready(self) -> bool:
        """Есть ли куда и с чем идти.

        Токен обязателен даже во внутреннем контуре: сеть — это транспорт, а не
        аутентификация. Без токена лид-бот всё равно ответит 401, и мы получим
        handoff на каждом диалоге — лучше честно считать это «не настроено».
        """
        return bool(self.url and self.token)


def _from_env() -> LeadbotConfig:
    return LeadbotConfig(
        url=settings.leadbot_url.strip(),
        token=settings.leadbot_token.strip(),
        source="env",
    )


#: Кэш процесса: (когда обновлён, что лежит). Пустой — ещё ни разу не читали.
_cache: tuple[float, LeadbotConfig] | None = None


def current() -> LeadbotConfig:
    """Конфигурация для немедленного использования, без обращения к базе.

    Синхронно и намеренно: сюда приходят из шага сценария, где своей сессии для
    настроек заводить нельзя — тик уже держит транзакцию диалога. Свежесть
    обеспечивает :func:`ensure_fresh`, который зовёт воркер перед тиком.
    """
    if _cache is not None:
        return _cache[1]
    return _from_env()


async def ensure_fresh(db: AsyncSession) -> LeadbotConfig:
    """Обновить кэш, если он устарел. Зовётся ПЕРЕД тиком бота."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    config = await load(db)
    _cache = (now, config)
    return config


def _drop_cache() -> None:
    """Забыть прочитанное — после сохранения и в тестах."""
    global _cache
    _cache = None


async def load(db: AsyncSession) -> LeadbotConfig:
    """Прочитать конфигурацию из базы; чего нет — берём из окружения.

    Запасной вариант из `.env` — не совместимость, а разделение ролей: инженер
    задаёт стартовое состояние при развёртывании, владелец переопределяет его
    из интерфейса, сброс возвращает к тому, что задал инженер.
    """
    row = await db.get(AppSetting, KEY)
    if row is None or not isinstance(row.value, dict):
        return _from_env()

    stored: dict[str, Any] = row.value
    fallback = _from_env()

    token = fallback.token
    raw = stored.get("token_enc")
    if isinstance(raw, str) and raw:
        try:
            token = crypto.decrypt_token(base64.b64decode(raw))
        except (crypto.DecryptError, ValueError):
            # Ключ шифрования сменился (восстановление копии на новом сервере) —
            # токен нечитаем. Молчать нельзя: иначе каждый диалог будет уходить
            # человеку «неизвестно почему», а причина ровно здесь.
            log.warning("leadbot.token_unreadable")
            token = ""

    url = stored.get("url")
    return LeadbotConfig(
        url=url.strip() if isinstance(url, str) and url.strip() else fallback.url,
        token=token,
        source="db",
    )


async def save(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    url: str,
    token: str | None,
) -> LeadbotConfig:
    """Сохранить адрес и токен. ``token=None`` — «оставить прежний».

    Отдельное значение для «не меняли» обязательно по той же причине, что у
    ключей Авито: экран не показывает токен, и без этого различия сохранение
    одного лишь адреса стирало бы токен пустой строкой — а заметили бы это
    только по тому, что автоответ вдруг перестал работать.
    """
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("Адрес лид-бота должен начинаться с http:// или https://")

    existing = await load(db)
    token = existing.token if token is None else token.strip()
    # Заголовки HTTP — latin-1, и токен с кириллицей роняет запрос ещё до сети.
    # Ловится это как «лид-бот недоступен» на КАЖДОМ диалоге, а искать причину
    # человек будет в сети и в чужом сервере. Между тем повод самый бытовой:
    # русская «с» вместо латинской при наборе руками или копирование из чата.
    if not token.isascii():
        raise ValueError("Токен лид-бота может состоять только из латиницы, цифр и знаков")

    value: dict[str, str] = {"url": url}
    if token:
        value["token_enc"] = base64.b64encode(crypto.encrypt_token(token)).decode()

    row = await db.get(AppSetting, KEY)
    if row is None:
        db.add(AppSetting(key=KEY, value=value, updated_by_id=actor_id))
    else:
        row.value = value
        row.updated_by_id = actor_id

    _drop_cache()
    # Адрес в журнале полезен (по нему видно, куда ушёл автоответ), токена нет.
    log.info("leadbot.saved", url=url, token_set=bool(token))
    return replace(existing, url=url, token=token, source="db")


async def reset(db: AsyncSession) -> LeadbotConfig:
    """Вернуться к тому, что задано на сервере в `.env`."""
    await db.execute(sa.delete(AppSetting).where(AppSetting.key == KEY))
    _drop_cache()
    log.info("leadbot.reset")
    return _from_env()


def safe_dump(config: LeadbotConfig) -> dict[str, Any]:
    """Что можно показать человеку и записать в журнал. ТОКЕНА ЗДЕСЬ НЕТ — никогда."""
    return {
        "url": config.url,
        "token_set": bool(config.token),
        "source": config.source,
        "ready": config.is_ready,
    }


async def seed_process(session_factory: Any, component: str) -> None:
    """Прочитать настройку из базы ОДИН РАЗ при старте процесса.

    У только что поднявшегося воркера кэш пуст, и до первого тика он считает
    правдой `.env` — то есть значение инженера, а не владельца. На Авито это уже
    стоило дня работы (`services/avito_app.py::seed_process`), и повторять урок
    на автоответе не хочется: расхождение выглядело бы как «бот отвечает не тем
    мозгом» и искали бы его в сценарии.

    Падать из-за этого нельзя: не прочиталось — работаем по `.env`, как раньше.
    """
    try:
        async with session_factory() as db:
            config = await ensure_fresh(db)
        log.info("leadbot.seeded", component=component, **safe_dump(config))
    except Exception:  # noqa: BLE001 — старт важнее свежести настройки
        log.warning("leadbot.seed_failed", component=component, exc_info=True)


# ----------------------------------------------------------------- клиент


def _request_id(
    bot: Any,
    dialog: list[dict[str, Any]],
    client_name: str = "",
    city: str = "",
    conv_key: str = "",
) -> str:
    """Ключ идемпотентности для лид-бота.

    ЗАЧЕМ. Лид-бот держит кэш ответов по `request_id` (10 минут): мы не ретраим,
    но сеть и балансировщик могут задвоить POST, а каждый лишний вызов — это его
    деньги и риск второго ответа клиенту.

    ПОЧЕМУ ХЭШ ДИАЛОГА, А НЕ ИДЕНТИФИКАТОР ДИАЛОГА. В протоколе `ai_answer` его
    просто нет — приходят бот, реплики и заголовок объявления. Зато хэш реплик
    обладает нужным свойством: у повторного POST того же тика он совпадает, а
    как только клиент напишет ещё раз, диалог изменится и ключ станет другим.
    """
    # ⚠ 16.08: имя и город — тоже вход (с 15.08 они меняют ответ бота). Без них
    # два клиента с одинаковым «Здравствуйте» в 10-минутном окне делили один
    # кэш — второй получал реплику с чужим именем и ценами чужого города.
    # ⚠ 17.08: conv_key (id диалога) — обязательная часть ключа. Клиент,
    # разославший ОДИН текст на десять аккаунтов, получал бы из кэша ОДИН
    # ответ во все десять чатов — мгновенная связка аккаунтов между собой.
    # ⚠ 30.08 (хвост №1 аудита): подряд идущие ДОСЛОВНЫЕ дубли реплик клиента
    # схлопываются до хэша — задвоенный вебхук Авито менял диалог («текст» →
    # «текст, текст»), ключ становился новым, и одна реплика порождала до
    # четырёх вызовов модели с разными ответами (боевой случай, 16 секунд).
    dedup: list[dict[str, Any]] = []
    for m in dialog:
        if (
            dedup
            and m.get("role") == dedup[-1].get("role") == "client"
            and (m.get("text") or "").strip() == (dedup[-1].get("text") or "").strip()
        ):
            continue
        dedup.append(m)
    material = json.dumps(
        [str(getattr(bot, "id", "")), conv_key, client_name, city, dedup],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _parse(data: Any) -> dict[str, Any] | None:
    """Ответ лид-бота → контракт шага. Всё, что не той формы, — `None`.

    Строгость здесь дешевле снисходительности: пропустив `reply=None` или
    `confidence="высокая"`, мы отправим клиенту пустое сообщение или посчитаем
    уверенность нулевой и отдадим человеку готовый хороший ответ. Оба исхода
    выглядят как «бот сломался», но искать их пришлось бы в движке.
    """
    if not isinstance(data, dict):
        return None
    reply = data.get("reply")
    needs_operator = data.get("needs_operator")
    confidence = data.get("confidence")
    # `bool` — подкласс `int`, поэтому его отсекаем явно: `confidence: true`
    # это мусор, а не единица.
    if (
        not isinstance(reply, str)
        or not isinstance(needs_operator, bool)
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
    ):
        return None
    answer: dict[str, Any] = {
        "reply": reply[:MAX_REPLY_CHARS],
        "confidence": max(0.0, min(1.0, float(confidence))),
        "needs_operator": needs_operator,
    }
    # Необязательные поля контракта движка (`engine.exec_ai_answer`): причина передачи
    # со сроком — в сводку передачи, «лид готов» — отдельной заметкой. Кладём ТОЛЬКО
    # когда есть что класть: пустой ключ в ответе означал бы, что бэкенд про это поле
    # что-то сказал, а он промолчал.
    meta = data.get("meta")
    comment = _handoff_comment(meta)
    if comment:
        answer["handoff_comment"] = comment
    note = _lead_note(meta)
    if note:
        answer["note"] = note
    # ⚠ ПРИЧИНА ПЕРЕДАЧИ — И ТОЛЬКО ОНА (26.08). `exec_ai_answer` берёт настоящую причину из
    # `meta.escalation.reason`; без неё очередь получает заглушку «AI не уверен в ответе» на
    # КАЖДУЮ передачу. Правка на стороне движка уже стояла, но данные до неё не доезжали:
    # здесь собирался ответ из трёх полей, и `meta` в него не попадала никогда. Снимок
    # владельца 26.08: бот отказался с причиной `not_primary`, а в ленте у диспетчера
    # написано «AI не уверен» — и рядом, заметкой, правильное «диалог уже ведёт человек».
    #
    # ⚠ ЦЕЛИКОМ `meta` НЕ ОТДАЁМ, И ЭТО НЕ ПЕДАНТИЗМ. В ней лежит `lead` с телефоном и
    # адресом клиента, а ответ шага уходит дальше по движку и попадает в записи, которые
    # читают шире, чем переписку (то же правило, что у `_log_meta`). Отдаём ровно то поле,
    # ради которого правка и делается.
    причина = ""
    вид_флага = ""
    лид_готов = False
    клиент_закрыл = False
    if isinstance(meta, dict):
        escalation = meta.get("escalation")
        if isinstance(escalation, dict):
            причина = _plain(escalation.get("reason"))
        # ⚠ `flag.kind` — ВТОРОЕ ПОЛЕ, БЕЗ КОТОРОГО ПРАВИЛО ВЛАДЕЛЬЦА НЕ РАБОТАЕТ.
        # Движок заканчивает диалог после окончательного отказа именно по нему
        # (`engine.py`: `meta.flag.kind == "refuse"` → do_handoff). Правка 27.08,
        # оставившая от `meta` одну лишь причину эскалации, вырезала и его —
        # и бот снова принялся дожимать клиента после собственного «не помогу»,
        # ровно как на снимке владельца 26.08 («зачем бот ведёт дальше диалог,
        # когда понятно, что тут матрица»).
        #
        # Отдаём ОДНО поле, а не флаг целиком: правило «не пропускать `meta`
        # наружу» остаётся в силе, там лежит `lead` с телефоном и адресом.
        флаг = meta.get("flag")
        if isinstance(флаг, dict):
            вид_флага = _plain(флаг.get("kind"))
        # ⚠ `client_closed` — КЛИЕНТ ЗАВЕРШИЛ РАЗГОВОР САМ (боевой диалог 29.08,
        # Пятигорск: «мне выезд не подходит» → бот попрощался, а диалог остался
        # живым в очереди). Движок по нему закрывает диалог с итогом «отказался».
        клиент_закрыл = bool(meta.get("client_closed"))
        # ⚠ `lead_ready` — ТРЕТЬЕ ПОЛЕ, И БЕЗ НЕГО ЗАКРЫТИЕ НЕ РАБОТАЕТ (29.08).
        # По нему движок закрывает диалог с собранной заявкой
        # (`engine.py`: `meta.lead_ready` → `close_by_leadbot`). Пропускаем
        # именно ФЛАГ, а не сам `lead`: там телефон и адрес клиента, и правило
        # «не пропускать `meta` наружу» остаётся в силе — булев признак ничего
        # о клиенте не рассказывает.
        лид_готов = bool(meta.get("lead_ready"))
    if причина or вид_флага or лид_готов or клиент_закрыл:
        answer["meta"] = {}
        if причина:
            answer["meta"]["escalation"] = {"reason": причина}
        if вид_флага:
            answer["meta"]["flag"] = {"kind": вид_флага}
        if лид_готов:
            answer["meta"]["lead_ready"] = True
        if клиент_закрыл:
            answer["meta"]["client_closed"] = True
    return answer


#: Потолок для текста, пришедшего из чужого сервиса. Заметка — не переписка: длинная
#: простыня в ленте диалога вытесняет то, ради чего в неё смотрят.
MAX_META_CHARS = 300


def _plain(text: Any) -> str:
    """Чужой текст, безопасный для нашей ленты.

    Фигурные скобки убираем НЕ из вкусовщины: комментарий сценария у нас проходит
    через подстановку, и одна `{` в чужой фразе превратилась бы в неизвестное поле,
    вместе с которым подстановщик выбрасывает всё предложение. Движок для внешних
    комментариев подстановку отключает (`render_comment=False`), но текст ходит и
    в заметку, и в будущие места, а рассчитывать на чужую аккуратность здесь незачем.
    """
    return str(text or "").replace("{", "(").replace("}", ")").strip()[:MAX_META_CHARS]


def _handoff_comment(meta: Any) -> str | None:
    """Причина передачи и срок реакции — словами, в сводку передачи.

    До этого они уходили только в журнал сервера: человек в диалоге видел «бот
    передал» и ни слова о том, почему и насколько срочно. Причина у лид-бота уже
    человеческая (`label`), поэтому свой словарь причин здесь не заводим — он
    разъехался бы с чужим при первом же изменении на той стороне.
    """
    if not isinstance(meta, dict):
        return None
    escalation = meta.get("escalation")
    if not isinstance(escalation, dict):
        return None
    label = _plain(escalation.get("label")) or _plain(escalation.get("reason"))
    if not label:
        return None
    parts = [f"Причина по регламенту: {label}"]
    deadline = escalation.get("deadline_min")
    if isinstance(deadline, int) and not isinstance(deadline, bool) and 0 < deadline <= 1440:
        parts.append(f"ответить за {deadline} мин")
    note = _plain(escalation.get("note"))
    if note:
        parts.append(f"клиент написал: «{note}»")
    return ". ".join(parts)


def _lead_note(meta: Any) -> str | None:
    """«Заявка собрана» — заметкой в ленту.

    Это НЕ эскалация, и мешать одно с другим нельзя: очередь претензий утонула бы
    в успешных заявках. Календаря у бота нет, слот ничем не подтверждён — поэтому
    заметка зовёт человека подтвердить время, а не рапортует об успехе.

    ⚠ ЗНАЧЕНИЯ НЕ ПЕЧАТАЕМ, только имена полей: в `lead` лежит телефон клиента, а
    заметку видно шире, чем переписку (её читает вся смена).
    """
    if not isinstance(meta, dict) or not meta.get("lead_ready"):
        return None
    lead = meta.get("lead")
    fields = ", ".join(sorted(_plain(k) for k in lead)) if isinstance(lead, dict) and lead else ""
    tail = f" Собрано: {fields}." if fields else ""
    return f"🤖 Лид-бот считает заявку собранной — подтвердите время у клиента.{tail}"


def _log_meta(meta: Any) -> None:
    """Побочные сведения из `meta` — в журнал, движку они не нужны.

    Причина эскалации и срок реакции нужны не движку, а людям в очереди: без
    причины её не отфильтровать, без срока не понять, что просрочено. «Лид
    готов» — вообще не эскалация, и в журнале он обязан отличаться, иначе
    успешные заявки перемешаются с претензиями.

    ⚠ ЗНАЧЕНИЯ ЛИДА НЕ ПИШЕМ — только имена полей. В `lead` лежит телефон
    клиента, а журнал читают шире, чем переписку.
    """
    if not isinstance(meta, dict):
        return
    escalation = meta.get("escalation")
    if isinstance(escalation, dict):
        log.info(
            "leadbot.escalation",
            reason=escalation.get("reason"),
            deadline_min=escalation.get("deadline_min"),
        )
    if meta.get("lead_ready"):
        lead = meta.get("lead")
        log.info(
            "leadbot.lead_ready",
            fields=sorted(lead) if isinstance(lead, dict) else None,
        )
    warnings = meta.get("warnings")
    if isinstance(warnings, list):
        for text in warnings:
            log.warning("leadbot.warning", text=str(text)[:300])


class LeadbotAI:
    """Реализация протокола `AIBackend` (`app/bots/engine.py`) поверх HTTP.

    Экземпляр дешёвый и без состояния: маршрутизатор создаёт его на вызов, беря
    свежие адрес и токен из :func:`current`. Держать долгоживущий объект с
    токеном внутри было бы хуже — настройку меняют из интерфейса, и «почему бот
    ходит по старому адресу» стало бы вопросом про время жизни объекта.
    """

    def __init__(self, url: str, token: str, *, budget_seconds: float | None = None) -> None:
        self._endpoint = url.rstrip("/") + ANSWER_PATH
        self._token = token
        # Бюджет тот же, что у Claude (`AI_TIMEOUT_SECONDS`, 05 §4): движок ждёт
        # шаг одинаково, кто бы за ним ни стоял. Лид-бот обязан ответить раньше
        # (docs/42: 9 секунд) и сам решить, что не успевает, — тогда передачу
        # человеку осмысленно назначает он, а не наш секундомер.
        self._budget = float(
            budget_seconds if budget_seconds is not None else settings.ai_timeout_seconds
        )

    def __repr__(self) -> str:
        # Не для красоты: `repr` уезжает в трассировку, трассировка — в Sentry.
        return f"<LeadbotAI {self._endpoint} token={'задан' if self._token else 'нет'}>"

    async def call_raw(
        self,
        dialog: list[dict[str, Any]],
        item_title: str | None,
        *,
        request_id: str,
        client_name: str = "",
        city: str = "",
        channel: dict[str, str] | None = None,
        prior_suggestions: list[str] | None = None,
        calls: int = 0,
        conv_key: str = "",
    ) -> LeadbotReply:
        """Один вызов лид-бота — СЕТЬ И НИЧЕГО КРОМЕ. Исключений не бросает.

        ЗАЧЕМ ОТДЕЛЬНО ОТ `ai_answer`. Один и тот же вызов нужен теперь троим:
        шагу сценария (там важен разобранный контракт), журналу работы (там
        важен ВЕСЬ ответ вместе с `meta` — слой, эскалация, предупреждения) и
        экрану «Лид-бот» с проверкой связи и тестовым разговором (там важна
        ещё и причина отказа человеческими словами). Три копии сетевого кода
        разошлись бы на первой же правке заголовков или таймаута.

        Возвращает :class:`LeadbotReply` всегда: «не дозвонились» — такой же
        ответ, как двухсотый, просто с заполненным `error`. Правило владельца
        №4 (недоступность ИИ не блокирует доставку) держится именно на том, что
        отсюда наружу не летит ничего.
        """
        payload = {
            "dialog": dialog,
            # ⚠ 30.08 (хвост №7 аудита): стабильный маркер диалога для теневого
            # журнала бота — без него нельзя собрать диалог целиком при разборе.
            # Хэш, а не сырой id: боту содержимое не нужно, только постоянство.
            "chat": hashlib.sha256((conv_key or "").encode()).hexdigest()[:12] if conv_key else "",
            "item_title": (item_title or "").strip(),
            # ⚠ 15.08: протокол расширен — движок передаёт имя клиента и город
            # (`ScenarioEngine._ai_request`). Город решает у бота филиал,
            # колонку прайса и окно приезда; имя — живое обращение в реплике.
            # Пустые строки законны: экран «Лид-бот» и тестовый разговор шлют
            # вызов без диалога-носителя, там контекста нет.
            "client_name": (client_name or "").strip(),
            "city": (city or "").strip(),
            # 16.08: канальные данные — источник в комментарий партнёра заявки,
            # ссылка отзыва в 🟢-хвост, номер партнёра решает «Отзыв» и белые
            "origin": ((channel or {}).get("origin") or "").strip(),
            # манера мастера этого канала (p1..p7): десять аккаунтов — десять голосов
            "persona": ((channel or {}).get("persona") or "").strip(),
            "partner": ((channel or {}).get("partner") or "").strip(),
            "review_url": ((channel or {}).get("review_url") or "").strip(),
            # ⚠ 24.08: то, чего нет в переписке. Подсказки бота лежат ЗАМЕТКАМИ и в
            # историю к нему не возвращаются — из-за этого он трижды просил номер при
            # собственном правиле «максимум две» и трижды повторял одну фразу. Звонок
            # лежит служебной записью, и о нём бот не знал вовсе: клиент набирал номер,
            # а чат просил у него телефон.
            # ⚠ ОТДЕЛЬНЫЕ ПОЛЯ, А НЕ ЧАСТЬ `dialog`: клиент подсказок не видел, и
            # выдавать их за переписку значит убедить бота, что он уже поздоровался.
            "prior_suggestions": [str(x) for x in (prior_suggestions or []) if str(x).strip()],
            "events": [{"type": "call"} for _ in range(max(0, int(calls or 0)))],
            "request_id": request_id,
        }
        started = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            # Стена — `wait_for`: у httpx свои таймауты на фазы, но общего потолка
            # на «соединился, читаю, читаю, читаю» он не даёт, а бюджет шага общий.
            async with httpx.AsyncClient(timeout=self._budget) as client:
                response = await asyncio.wait_for(
                    client.post(
                        self._endpoint,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._token}"},
                    ),
                    timeout=self._budget,
                )
        except TimeoutError:
            log.warning("leadbot.unavailable", error="TimeoutError", request_id=request_id)
            return LeadbotReply(
                request_id=request_id,
                ms=_ms(),
                error=f"Не ответил за {self._budget:.0f} с",
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
            # Тип, а не текст: в тексте httpx приводит адрес, а нам важно только
            # «не дозвонились». Токена нет ни там, ни там — он в заголовке.
            log.warning("leadbot.unavailable", error=type(exc).__name__, request_id=request_id)
            return LeadbotReply(request_id=request_id, ms=_ms(), error="Не дозвонились до лид-бота")

        if response.status_code != 200:
            # 401 отдельной строкой: это не «упал», это «не пустили», и лечится
            # оно настройкой, а не перезапуском. Без этого различия искать
            # причину пришлось бы в логах чужого сервера.
            log.warning(
                "leadbot.rejected" if response.status_code == 401 else "leadbot.bad_status",
                status=response.status_code,
                request_id=request_id,
            )
            return LeadbotReply(
                request_id=request_id,
                ms=_ms(),
                status=response.status_code,
                error=(
                    "Лид-бот не принял токен"
                    if response.status_code == 401
                    else f"Лид-бот ответил {response.status_code}"
                ),
            )

        try:
            data = response.json()
        except ValueError:
            log.warning("leadbot.bad_json", request_id=request_id)
            return LeadbotReply(
                request_id=request_id,
                ms=_ms(),
                status=response.status_code,
                error="Ответ не разобрался как JSON",
            )

        answer = _parse(data)
        if answer is None:
            # Форму пишем, содержимое — нет: в ответе текст переписки с клиентом.
            log.warning(
                "leadbot.bad_shape",
                keys=sorted(data)[:10] if isinstance(data, dict) else None,
                request_id=request_id,
            )
            return LeadbotReply(
                request_id=request_id,
                ms=_ms(),
                status=response.status_code,
                raw=data if isinstance(data, dict) else None,
                error="Ответ не той формы — контракт нарушен",
            )

        return LeadbotReply(
            request_id=request_id,
            ms=_ms(),
            status=response.status_code,
            answer=answer,
            raw=data if isinstance(data, dict) else None,
        )

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
        prior_suggestions: list[str] | None = None,
        calls: int = 0,
    ) -> dict[str, Any] | None:
        """Ответ лид-бота. `None` — вызывающий обязан сделать handoff (02 §3.3)."""
        if not dialog:
            return None

        reply = await self.call_raw(
            dialog,
            item_title,
            request_id=_request_id(
                bot, dialog, client_name=client_name, city=city, conv_key=conv_key
            ),
            client_name=client_name,
            city=city,
            channel=channel,
            prior_suggestions=prior_suggestions,
            calls=calls,
            conv_key=conv_key,
        )
        # СЛЕД ОБРАЩЕНИЯ КЛАДЁТСЯ ЗДЕСЬ, а не у вызывающего: только тут видно и
        # запрос, и весь ответ. Складывать его в движок значило бы тащить туда
        # `meta`, которая движку не нужна ни одним полем.
        last_call.set(reply)
        if reply.answer is None:
            return None
        _log_meta(reply.raw.get("meta") if reply.raw else None)
        return reply.answer

    async def classify_message(self, texts: list[str]) -> dict[str, Any] | None:
        """Классификации у лид-бота нет — её роль там играют роутеры приёма.

        `None` здесь означает «нечем классифицировать», и это штатная ветка:
        движок просто не проставит метку негатива. Но в связке через
        `app/bots/provider.py` сюда не приходят вовсе — классификатор остаётся
        нашим, чтобы подключение автоответа не отняло у диспетчеров метку
        «негатив» и распознавание просьбы позвать человека.
        """
        return None
