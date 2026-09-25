"""ScenarioEngine — машина состояний сценария (02 §2.3).

Один тик = `on_incoming`/`on_timeout` + `run()`. Движок работает ТОЛЬКО с
сессией БД: публикация в Pub/Sub и постановка ARQ-задач откладываются в
:class:`~app.bots.state.Outbox` и выполняются вызывающим строго после commit'а
(08 §8.1). Благодаря этому один и тот же класс обслуживает и воркер
(`app/bots/runtime.py`), и песочницу редактора (02 §5.3), и юнит-тесты — без
Redis и без ARQ.

Внешнее время инжектируется (`now=`): «сейчас ночь» проверяется и в песочнице,
и в тестах без `time_machine`.

AI-подсистема подключается через `ai=`; отсутствие модуля, таймаут и любая
ошибка API означают одно и то же — `handoff(ai_unavailable)`. Недоступность AI
НИКОГДА не блокирует доставку сообщений (решение владельца №4, DESIGN §4.5).
Кто именно отвечает — Claude (`app/bots/ai.py`) или лид-бот владельца
(`app/bots/leadbot.py`) — решает `app/bots/provider.py` по полю бота
`ai_provider`; движку эта разница не видна.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import handoff as handoff_mod
from app.bots import leadbot
from app.bots.handoff import (
    BOT_CLOSED_ACTION,
    NEGATIVE_TAG,
    add_bot_message,
    add_conversation_tags,
    add_note,
    conversation_updated_event,
    message_new_event,
)
from app.bots.provider import LEADBOT, provider_of
from app.bots.state import BotState, Outbox, utcnow
from app.bots.steps import (
    Limits,
    Rendered,
    Scenario,
    Step,
    detect_human_request,
    extract_phone,
    mask_phones,
    match_menu_option,
    parse_timeout,
    pick_condition_branch,
    render,
    validate_answer,
)
from app.core import trace
from app.integrations.avito.listing_url import parse_listing_url
from app.models import AvitoAccount, Client, Conversation, Message
from app.models.leadbot import (
    OUTCOME_BRIDGE as LB_BRIDGE,
)
from app.models.leadbot import (
    OUTCOME_HANDOFF as LB_HANDOFF,
)
from app.models.leadbot import (
    OUTCOME_HINT as LB_HINT,
)
from app.models.leadbot import (
    OUTCOME_LOW_CONFIDENCE as LB_LOW_CONFIDENCE,
)
from app.models.leadbot import (
    OUTCOME_SENT as LB_SENT,
)
from app.models.leadbot import (
    OUTCOME_UNAVAILABLE as LB_UNAVAILABLE,
)
from app.services import conversation_status as status_dict
from app.services import leadbot_log
from app.services.audit import write_audit
from app.services.avito_cities import city_by_slug
from app.services.conversations import clear_closed_marks
from app.services.messages import MAX_TEXT_LENGTH

log = structlog.get_logger("app.bots.engine")

AI_UNAVAILABLE_NOTE = "🤖 AI недоступен, передал диалог менеджеру"
CLOSE_NOTE = "🤖 Диалог закрыт ботом (шаг {step})"
#: Шаг close дошёл до диалога, который ведёт человек, или идёт подсказкой.
CLOSE_SKIPPED_NOTE = (
    "🤖 Сценарий дошёл до закрытия (шаг {step}) — диалог ведёт человек, бот его не закрыл"
)
CLASSIFY_CONTEXT_MESSAGES = 3  # последние сообщения клиента для классификатора (02 §3.4)

#: Заметка сотрудникам, когда сообщение шага целиком съела пустая подстановка.
EMPTY_RENDER_NOTE = (
    "🤖 Шаг {step}: сообщение не отправлено — подставлять нечего ({placeholders}). "
    "Проверьте текст шага в настройках бота"
)
#: Комментарий к передаче, когда пустой оказалась подстановка в ВОПРОСЕ.
EMPTY_QUESTION_COMMENT = (
    "Вопрос шага {step} составить не из чего: пусто в {placeholders}. "
    "Ждать ответа на незаданный вопрос бот не стал"
)


@runtime_checkable
class AIBackend(Protocol):
    """Контракт `app/bots/ai.py` (02 §3) — пишется в параллельной зоне.

    Все три метода best-effort: `None` означает «модель недоступна», и это
    штатная ветка, а не ошибка.
    """

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
    ) -> dict[str, Any] | None: ...

    async def classify_message(self, texts: list[str]) -> dict[str, Any] | None: ...


_UNSET = object()


@dataclass
class AiRequest:
    """Вызов модели, вынесенный за транзакцию тика (проверка 24.09).

    Тик доходит до шага `ai_answer`, собирает контекст и останавливается: ответа
    модели (до 15 секунд) он ждёт уже без блокировки строки диалога. Здесь всё,
    что нужно, чтобы продолжить шаг во второй транзакции (`runtime`).
    """

    step_id: str
    backend: Any
    bot: Any
    dialog: list[dict[str, Any]]
    item_title: str | None
    kwargs: dict[str, Any]
    #: Последнее входящее, по которому собран контекст. Появилось новое — ответ
    #: устарел (маркер свежести 30.08).
    last_in_id: Any
    #: Последняя реплика клиенту. Появилась новая (ответил оператор) — тоже.
    last_out_id: Any = None
    #: Что держал движок между шагами: текст серии для условий, её сообщения,
    #: текст для классификатора, «одна подсказка на входящее».
    last_incoming: str | None = None
    series: list[str] = field(default_factory=list)
    pending_classification: str | None = None
    suggest_once: bool = False

    async def ask(self) -> dict[str, Any] | None:
        """Поход в модель. Любая ошибка — недоступность (02 §3.3), не ретраим."""
        try:
            result = await self.backend.ai_answer(
                self.bot, self.dialog, self.item_title, **self.kwargs
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
            log.warning("bot.ai_answer_failed", error=type(exc).__name__)
            return None
        return result if isinstance(result, dict) else None


@dataclass
class AiAnswer:
    """Ответ модели на отложенный вызов — для второй транзакции тика."""

    request: AiRequest
    result: dict[str, Any] | None


def suggest_entry_step(scenario: Any, *, fresh: bool, conv_status: str) -> str | None:
    """Куда войти в режиме ПОДСКАЗОК (решение владельца 21.08).

    Свежий бот в новом диалоге ведёт сценарий с начала — приветствие там уместно.
    Во всех остальных случаях (разговор уже идёт, оператор отвечал, бот раньше
    передал диалог) подсказка обязана быть черновиком ОТВЕТА ПО КОНТЕКСТУ, а не
    «здравствуйте» посреди беседы: возвращаем первый шаг типа ``ai_answer``.
    ``None`` — сценарий без ai-шага, идём прежней дорогой.
    """
    if fresh and (conv_status or "") == "new":
        return None
    for step_id in getattr(scenario, "order", ()):  # порядок объявления в сценарии
        step = scenario.step(step_id)
        if step is not None and step.type == "ai_answer":
            return step_id
    return None


#: Сколько картинок из одного сообщения имеет смысл передавать. Зрение лид-бота
#: берёт из реплики не больше четырёх (brain/server.py, _IMG_PER_MSG); лишние ссылки
#: только раздувают запрос и ничего не добавляют.
MAX_IMAGE_LINKS = 4


def _attachments_line(attachments: Any, *, with_urls: bool) -> str:
    """Вложения сообщения -> строка для истории, уходящей модели.

    ⚠ ЗАЧЕМ ЭТО ВООБЩЕ. Сообщение, где клиент прислал ТОЛЬКО фото или голосовое,
    имеет пустое тело: содержимое лежит в `attachments`. Раньше такие сообщения
    вырезались из истории целиком, и бот отвечал так, будто последним словом
    клиента было предыдущее текстовое. По живой выгрузке подсказок это 8.8%
    реплик клиента — каждая двенадцатая подсказка давалась вслепую.

    Картинка со ссылкой отдаётся видом «🖼 URL»: ровно этот вид разбирает зрение
    лид-бота — оно скачивает файл само и показывает модели картинкой. Всё
    остальное (голосовое, документ, картинка без ссылки) превращается в честную
    подпись: бот не увидит содержимого, но будет знать, что клиент что-то прислал,
    и не станет переспрашивать в пустоту.

    with_urls=False — дорога СВОЕЙ модели, где история маскируется. Туда уходит
    только подпись: согласия выгружать фотографии клиента наружу нет, а знание
    «пришло фото» ценно и без ссылки.
    """
    if not isinstance(attachments, list) or not attachments:
        return ""
    куски: list[str] = []
    ссылок = лишних = 0
    for att in attachments:
        if not isinstance(att, dict):
            continue
        url = att.get("url") if isinstance(att.get("url"), str) else ""
        картинка = (att.get("kind") or "") == "image"
        if картинка and url and with_urls:
            if ссылок < MAX_IMAGE_LINKS:
                куски.append("🖼 " + url)
                ссылок += 1
            else:
                лишних += 1
            continue
        имя = att.get("name")
        if not isinstance(имя, str) or not имя.strip():
            имя = "Фотография" if картинка else "Вложение"
        куски.append(("🖼 " if картинка else "📎 ") + имя.strip())
    if лишних:
        куски.append(f"(и ещё {лишних} фото)")
    return "\n".join(куски)


#: Префикс заметки-подсказки. Был вписан строкой в двух местах — этого хватает,
#: чтобы однажды разъехаться и сравнивать подсказку с чужим текстом.
SUGGEST_PREFIX = "Подсказка бота: "
#: ⚠ ПРИЗНАК ЗВОНКА В СЛУЖЕБНОЙ ЗАПИСИ. Текст задаёт адаптер Авито
#: (`_CONTENT_FREE_KINDS["appCall"]` = «Клиент звонил через приложение Авито»), и
#: сверять по всей строке значило бы связать два модуля дословной фразой. Берём
#: корень: он переживёт и правку формулировки, и заглавную букву.
_CALL_MARK = "звонил"
#: Автор служебной записи о звонке. Тот же корень «звонил» стоит и в НАШЕМ
#: словаре причин передачи (`handoff.HANDOFF_REASONS["call_made"]`), поэтому
#: одного слова мало: без автора бот считал свою же запись звонком клиента.
_AVITO_SENDER = "avito"


def город_диалога(conv: Any) -> str:
    """Город клиента для лид-бота: слаг карточки, а если его нет — ссылка объявления.

    ⚠ ЗАПАСНОЙ ПУТЬ БЫЛ ВЕЗДЕ, КРОМЕ БОТА. `services/conversations.py` давно читает
    `conv.item_city_slug or parse_listing_url(conv.item_url).city_slug`; путь бота
    единственный обходился без него — и цена промаха у него самая высокая.

    Замер боевой базы 26.08: слаг пуст у 4832 диалогов из 15 970 (30 %), ссылка на
    объявление есть у 4710 из них (97,5 %), и город лежит прямо в ней —
    `avito.ru/tuapse/…`, `avito.ru/surgut/…`. Без города бот не имеет права называть
    окно приезда («час-полтора» верно только в черте города), а окно первым ходом —
    сильнейший ход воронки: 26,0 % до телефона против 11,7 % у хода без шага
    (23 159 первых ходов операторов). На каждом третьем диалоге бот молчал о времени
    на ровном месте.

    ⚠ СЛАГ ИМЕЕТ ПРИОРИТЕТ НАД ССЫЛКОЙ. Слаг — снимок на момент обращения; объявление
    могло переехать в другой город, и переписывать историю задним числом нельзя
    (docs/32). Ссылка — только замена пустоте.

    ⚠ ОТДЕЛЬНОЙ ФУНКЦИЕЙ, А НЕ СТРОКОЙ ВНУТРИ `_ai_request`: строку внутри метода
    тест может проверить только копией этой же строки, а копия расходится с оригиналом
    на первой правке. Здесь тест зовёт ровно тот код, который работает в бою.
    """
    слаг = (
        getattr(conv, "item_city_slug", None)
        or parse_listing_url(getattr(conv, "item_url", None)).city_slug
    )
    return city_by_slug(слаг) or ""


class ScenarioEngine:
    """Исполнитель сценария в одном диалоге."""

    def __init__(
        self,
        *,
        bot: Any,
        conv: Conversation,
        state: BotState,
        db: AsyncSession,
        outbox: Outbox | None = None,
        ai: Any = _UNSET,
        now: Callable[[], datetime] | datetime | None = None,
        scenario: Mapping[str, Any] | None = None,
        defer_ai: bool = False,
    ) -> None:
        self.bot = bot
        #: Тик бота ставит True: шаг `ai_answer` не зовёт модель сам, а оставляет
        #: `deferred_ai` и останавливает сценарий (см. :class:`AiRequest`).
        #: Песочница и тесты движка зовут модель на месте, как раньше.
        self.defer_ai = defer_ai
        self.deferred_ai: AiRequest | None = None
        self.prefetched_ai: AiAnswer | None = None
        self.conv = conv
        #: Реплики последнего вызова ИИ — источник вопроса для журнала лид-бота.
        self._last_ai_dialog: list[dict[str, Any]] = []
        self.state = state
        self.db = db
        self.outbox = outbox if outbox is not None else Outbox()
        self.scenario = Scenario.from_dict(
            scenario if scenario is not None else getattr(bot, "scenario", None)
        )
        self.limits: Limits = self.scenario.limits
        self._ai: Any = ai
        self._clock = now
        self._system_values: dict[str, Any] | None = None
        # Текст последнего входящего — нужен условиям `text_contains`/`text_matches`
        # и классификатору негатива.
        self.last_incoming: str | None = None
        # Сообщение, которое надо отдать классификатору ПОСЛЕ commit'а (02 §3.4).
        self.pending_classification: str | None = None
        # Ссылка шага указала в никуда: handoff требует await, а _goto — нет.
        self._broken_ref = False
        # Серия входящих по сообщениям (см. :meth:`on_incoming`).
        self.series: list[str] = []

    # ------------------------------------------------------------ окружение

    @property
    def bot_id(self) -> Any:
        return getattr(self.bot, "id", None)

    def now(self) -> datetime:
        if callable(self._clock):
            return self._clock()
        if isinstance(self._clock, datetime):
            return self._clock
        return utcnow()

    @property
    def ai(self) -> Any:
        """AI-бэкенд: инжектированный, иначе `app.bots.provider` (может не быть).

        Раньше здесь напрямую подтягивался `app.bots.ai`. Теперь по умолчанию
        стоит маршрутизатор: у бота есть поле `ai_provider`, и на шаге
        `ai_answer` за него может думать не Claude, а лид-бот владельца.
        Движку эта разница не видна — протокол один и тот же, — но выбирать
        поставщика он не должен, иначе слово «лид-бот» просочилось бы в машину
        состояний, которой до него нет никакого дела.
        """
        if self._ai is _UNSET:
            try:
                # importlib, а не `from app.bots import provider`: модуль тянет
                # за собой SDK модели и httpx, а движок обязан подниматься и без
                # них — им пользуются песочница и тесты, где сети нет вовсе.
                self._ai = importlib.import_module("app.bots.provider")
            except Exception as exc:  # noqa: BLE001 — без AI движок работает, просто уходит в handoff
                log.warning("bot.ai_module_missing", error=type(exc).__name__)
                self._ai = None
        return self._ai

    async def system_values(self) -> dict[str, Any]:
        """`{client_name}`, `{item_title}`, `{item_price}`, `{account_title}` (02 §1.2)."""
        if self._system_values is None:
            client = await self.db.get(Client, self.conv.client_id)
            account = await self.db.get(AvitoAccount, self.conv.account_id)
            self._system_values = {
                "client_name": (getattr(client, "name", None) or ""),
                "item_title": (self.conv.item_title or ""),
                "item_price": (self.conv.item_price or ""),
                "account_title": (getattr(account, "title", None) or ""),
            }
        return dict(self._system_values)

    async def context_values(self) -> dict[str, Any]:
        """Полное пространство имён подстановки: системные + собранные."""
        values = await self.system_values()
        values.update(self.state.vars)
        return values

    async def render(self, template: str | None) -> str:
        return (await self.render_full(template)).text

    async def render_full(self, template: str | None) -> Rendered:
        """Подстановка вместе с отчётом о выброшенном (02 §1.2)."""
        return render(template, await self.context_values())

    # ------------------------------------------------------------ исходящие

    @property
    def is_suggest_mode(self) -> bool:
        """Режим «подсказка»: клиенту не уходит ничего, ответ видит только оператор."""
        return (getattr(self.bot, "mode", "suggest") or "suggest") == "suggest"

    @staticmethod
    def is_double_post(text: str, prev: str) -> bool:
        """Дословный ли это повтор предыдущей реплики бота.

        Сравниваем ПОСЛЕ нормализации пробелов и регистра — «Здравствуйте,  соберу»
        и «здравствуйте, соберу» для клиента одно и то же. Похожие формулировки НЕ
        трогаем: бот варьирует их нарочно (несвязываемость каналов), и резать
        похожее значило бы ломать эту работу.
        """
        нов = " ".join((text or "").split()).lower()
        ст = " ".join((prev or "").split()).lower()
        return bool(нов) and нов == ст

    async def last_bot_text(self) -> str:
        """Реплика бота, ПОСЛЕ которой клиент ничего не написал.

        Именно она и означает даблпост: сказать второй раз то же самое, не услышав
        в ответ ни слова. Если клиент с тех пор написал — повтор законен и молчать
        нельзя: клиент, вернувшийся в закрытый диалог, здоровается заново, и второе
        «Здравствуйте!» ему полагается.

        В режиме подсказки реплика бота лежит заметкой с префиксом, в авто —
        обычным исходящим. Берём соответствующую режиму: иначе после смены режима
        сравнение шло бы не с тем.
        """
        if self.db is None or self.conv is None:
            return ""
        своё = (
            and_(Message.direction == "note", Message.body.like(SUGGEST_PREFIX + "%"))
            if self.is_suggest_mode
            else and_(Message.direction == "out", Message.sender_type == "bot")
        )
        запрос = (
            select(Message)
            .where(
                Message.conversation_id == self.conv.id,
                or_(своё, and_(Message.direction == "in", Message.sender_type == "client")),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        row = (await self.db.execute(запрос)).scalar_one_or_none()
        тело = getattr(row, "body", "") or ""
        if not isinstance(тело, str) or getattr(row, "sender_type", "") == "client":
            return ""
        return тело[len(SUGGEST_PREFIX) :] if тело.startswith(SUGGEST_PREFIX) else тело

    async def own_texts_since_client(self, limit: int = 20) -> list[str]:
        """Всё, что бот сказал ПОСЛЕ последней реплики клиента.

        ⚠ ОДНОЙ ПРЕДЫДУЩЕЙ РЕПЛИКИ МАЛО (живой случай 26.08). В ленте подряд стояли
        подсказка A, дожим сценария, снова подсказка A — дословная копия. Гард сравнивал
        только с непосредственно предыдущей, а между двумя A вклинился дожим, и копия
        прошла. Настоящее свойство не «не повторяй последнее», а «не говори второй раз
        то же самое, пока клиент не ответил»: пока он молчит, всё сказанное остаётся
        для него одинаково новым, и порядок реплик тут ни при чём.
        """
        if self.db is None or self.conv is None:
            return []
        своё = (
            and_(Message.direction == "note", Message.body.like(SUGGEST_PREFIX + "%"))
            if self.is_suggest_mode
            else and_(Message.direction == "out", Message.sender_type == "bot")
        )
        запрос = (
            select(Message)
            .where(
                Message.conversation_id == self.conv.id,
                or_(своё, and_(Message.direction == "in", Message.sender_type == "client")),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        out: list[str] = []
        for row in (await self.db.execute(запрос)).scalars():
            if getattr(row, "sender_type", "") == "client":
                break
            тело = getattr(row, "body", "") or ""
            if not isinstance(тело, str):
                continue
            out.append(тело[len(SUGGEST_PREFIX) :] if тело.startswith(SUGGEST_PREFIX) else тело)
        return out

    @staticmethod
    def _слова(text: str) -> set[str]:
        return set(re.findall(r"[а-яёa-z]{3,}", (text or "").lower()))

    @staticmethod
    def _первая_фраза(text: str) -> str:
        куски = re.split(r"[.!?\n]", (text or "").strip(), maxsplit=1)
        return " ".join(куски[0].split()).lower() if куски else ""

    @classmethod
    def is_near_duplicate(cls, text: str, prev: str, порог: float = 0.45) -> bool:
        """Та же мысль другими словами, пока клиент молчит.

        ⚠ ДОСЛОВНОГО СРАВНЕНИЯ МАЛО (снимок владельца 26.08):

            03:10  «Понимаю, цену скажу до начала работ, решать вам. Кстати, я на выезд
                    работаю, мастерской нет. Куда подъехать?»
            03:11  «Понимаю, цену скажу до начала работ, решать вам. Только я выездной
                    мастер, мастерской нет, сам к вам подъеду. Куда, электросталь?»

        Для гарда это разные строки, для человека — одно и то же дважды.

        ДВА ПРИЗНАКА, И ПЕРВЫЙ ТОЧНЕЕ. Одинаковая ПЕРВАЯ ФРАЗА — сама по себе повтор:
        в снимке это дословное «Понимаю, цену скажу до начала работ, решать вам».
        Ложных срабатываний у неё почти нет, а ловит она ровно тот случай, который
        владелец прислал.

        ПОРОГ 0,45 ВЫБРАН ЗАМЕРОМ, А НЕ НА ГЛАЗ. По 726 парам соседних боевых подсказок
        сходство ≥ 0,9 у 1,0 % пар, ≥ 0,7 у 2,1 %, ≥ 0,6 у 3,2 %, ≥ 0,5 у 5,2 %. Полосу
        0,34–0,56 прочитал глазами: почти всё там — один и тот же ход, сказанный дважды
        («антенна уличная или комнатная?», «картридж заправляли?», «марку и модель
        подскажите?»). Пара со снимка даёт 0,50 — порог 0,6 её пропускал.

        ⚠ АСИММЕТРИЯ ЦЕНЫ ОШИБКИ, И ОНА ОСОЗНАННАЯ. Ложное срабатывание = оператор не
        получит вторую подсказку, пока клиент молчит; первая при этом стоит. Пропуск =
        владелец второй раз видит, что бот «двоит». Второе дороже, поэтому порог ниже
        середины полосы.

        ⚠ КОРОТКИЕ РЕПЛИКИ СРАВНИВАЕМ ТОЛЬКО ДОСЛОВНО. На трёх словах Жаккар шумит:
        «Хорошо» и «Хорошо, наберу» дают 0,5 и повтором не являются.
        """
        A, B = cls._слова(text), cls._слова(prev)
        if len(A) < 4 or len(B) < 4:
            return False
        перв = cls._первая_фраза(text)
        if перв and len(перв) >= 12 and перв == cls._первая_фраза(prev):
            return True
        return len(A & B) / len(A | B) >= порог

    async def is_repeat_since_client(self, text: str) -> bool:
        """Говорил ли бот это же — дословно или той же мыслью, — пока клиент молчит."""
        for было in await self.own_texts_since_client():
            if self.is_double_post(text, было) or self.is_near_duplicate(text, было):
                return True
        return False

    async def send_bot_message(self, text: str) -> Message | None:
        """Единая точка исходящих бота (02 §2.3).

        Доставки не ждём: `deliver_message` асинхронна и ретраится сама
        (08 §3). Задача ставится после commit'а — из outbox.

        ⚠ РЕЖИМ «ПОДСКАЗКА» (миграция 0035). Если бот работает подсказкой, эта же точка
        не отправляет ничего клиенту, а кладёт готовый текст ЗАМЕТКОЙ в диалог: оператор
        видит предложенный ответ и решает сам. Перехват стоит именно здесь, в единственной
        точке исходящих, — иначе любой новый шаг сценария пришлось бы учить режиму заново,
        и однажды кто-нибудь забыл бы.
        """
        # ⚠ ДОСЛОВНЫЙ ПОВТОР (21.08). В живой выгрузке 5 подсказок из 924 — точная
        # копия того, что бот уже сказал в этом диалоге, а пинг «Ну что, подскажете?»
        # повторился четырежды подряд. Оператору это шум, клиенту в авто-режиме —
        # двойная отправка. Гард стоит в единственной точке исходящих, чтобы новый
        # шаг сценария получал его сам и никто не забыл его добавить.
        if await self.is_repeat_since_client(text):
            log.info("bot.double_post_skipped", conversation_id=str(self.conv.id))
            return None
        # Обрезаем ДО всего остального: и в базу, и в трассировку песочницы
        # обязан уходить ровно тот текст, который увидит клиент. Пока обрезка
        # жила только в аргументе add_bot_message, песочница показывала админу
        # длинный ответ модели целиком, а клиенту ехали первые 4000 символов.
        body = (text or "").strip()[:MAX_TEXT_LENGTH]
        if not body:
            return None
        msg = (
            await self.add_note(SUGGEST_PREFIX + body)
            if self.is_suggest_mode
            else self._отправить_клиенту(body)
        )
        # ⚠ СЧЁТ ТОЛЬКО ПО ФАКТУ ЗАПИСИ, И РОВНО ЗДЕСЬ.
        #
        # Счётчик рос в каждой ветке своей строкой, и в подсказке — ДО записи:
        # `+= 1`, а следом `add_note`, который возвращает None, если такая же
        # заметка уже лежит (`_заметка_уже_есть`). Реплики нет, счёт есть.
        #
        # Чем это опасно шире одной ветки: `bot_msgs_row` — предохранитель
        # «не заваливать клиента» (см. `max_bot_messages_row`), и фантомный
        # счёт закрывает боту рот за реплики, которых он не говорил. А в
        # `bot_vars` остаётся след, по которому потом ищут причину: на бою
        # 127 диалогов с `bot_msgs_row > 0` не имеют в `messages` ни одной
        # реплики бота (замер 08.09; окно 19–23.08, беда закрыта выкаткой
        # 23.08 — но след остался и читается как «бот отвечал»).
        #
        # Точка одна намеренно: следующий режим («подсказка» была таким же
        # новым режимом) получит правило сам, а не будет учить его заново.
        if msg is not None:
            self.state.counters.bot_msgs_row += 1
        return msg

    def _отправить_клиенту(self, body: str) -> Message:
        """Обычный режим: исходящее в ленту и в доставку. Текст уже готов."""
        moment = self.now()
        msg = add_bot_message(self.db, self.conv, body, now=moment)
        self.conv.last_message_at = moment
        self.conv.updated_at = moment
        self.outbox.job("deliver_message", msg.id, job_id=f"deliver:{msg.id}")
        self.outbox.event("message:new", message_new_event(self.conv, msg))
        self.outbox.note("bot_message", text=body)
        return msg

    async def _заметка_уже_есть(self, body: str) -> bool:
        """Ложилась ли ДОСЛОВНО такая же заметка, пока клиент молчит.

        ⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА. Гард повторов стоит в `send_bot_message` и
        смотрит на подсказки и исходящие бота; служебные заметки идут мимо него,
        через `add_note`, где не было проверок вовсе. Отсюда живой случай: пока
        лид-бот недоступен, заметка «ИИ недоступен» ложилась в ленту на КАЖДОЕ
        сообщение клиента — она пишется до передачи и вне её идемпотентности, а
        сама передача при повторе выходит сразу. Диалог зарастал одинаковыми
        строками, и настоящие заметки в них тонули.

        Сравниваем дословно и только до последней реплики клиента: ответил
        человек — обстоятельства изменились, и та же заметка снова осмысленна.
        """
        if self.db is None or self.conv is None:
            return False
        запрос = (
            select(Message)
            .where(
                Message.conversation_id == self.conv.id,
                or_(
                    Message.direction == "note",
                    and_(Message.direction == "in", Message.sender_type == "client"),
                ),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(20)
        )
        for row in (await self.db.execute(запрос)).scalars():
            if getattr(row, "sender_type", "") == "client":
                return False
            if (getattr(row, "body", "") or "").strip() == body:
                return True
        return False

    async def add_note(self, text: str) -> Message | None:
        body = (text or "").strip()
        if not body:
            return None
        if await self._заметка_уже_есть(body):
            log.info(
                "bot.note_skipped",
                conversation_id=str(getattr(self.conv, "id", "")),
                reason="repeat",
            )
            return None
        msg = add_note(self.db, self.conv, body, now=self.now())
        self.conv.updated_at = self.now()
        self.outbox.event("message:new", message_new_event(self.conv, msg))
        self.outbox.note("note", text=body)
        return msg

    # ------------------------------------------------------------- handoff

    async def do_handoff(
        self,
        reason: str,
        comment: str | None = None,
        tags: Iterable[str] = (),
        *,
        render_comment: bool = True,
    ) -> None:
        """Обёртка над :func:`app.bots.handoff.do_handoff` (идемпотентна).

        ``render_comment=False`` — для текста, пришедшего ИЗВНЕ, а не из сценария.
        Комментарий шага `handoff` пишет автор сценария, и подстановка в нём
        осмысленна: «Клиент {client_name} ждёт». Комментарий от AI-подсистемы —
        уже готовая фраза, и прогонять её через шаблонизатор опасно: одна фигурная
        скобка в чужом тексте превращается в неизвестную подстановку, а вместе с
        ней подстановщик выбрасывает ВСЁ предложение. Причина передачи исчезла бы
        молча — то есть ровно то, ради чего её сюда и несли.
        """
        if self.state.handoff_done():
            self.state.next_step = None
            return
        entities = await self.extract_entities()
        await handoff_mod.do_handoff(
            self.db,
            self.conv,
            self.state,
            self.outbox,
            reason=reason,
            bot_id=self.bot_id,
            comment=(await self.render(comment) if render_comment else comment)
            if comment
            else None,
            tags=tags,
            entities=entities,
            now=self.now(),
            suggest=self.is_suggest_mode,
        )

    # -------------------------------------------------------------- входящее

    async def on_incoming(self, text: str | None, *, series: Sequence[str] = ()) -> None:
        """Пред-обработка входящего клиента (02 §2.3).

        `text` — вся серия сообщений одной строкой (её собирает тик), `series` —
        она же по сообщениям: ответ на меню сверяется с каждым, иначе «ну» + «2»
        не попадали ни в один вариант (проверка 24.09).
        """
        self.series = list(series)
        s = self.state
        fresh = s.is_fresh()
        s.start(bot_id=self.bot_id, now=self.now())
        incoming = text or ""
        self.last_incoming = incoming
        s.counters.bot_msgs_row = 0  # клиент ответил — серия бота прервана

        # --- детекторы, работающие на КАЖДОМ входящем (02 §4) ---
        if detect_human_request(incoming):  # условие №1, эшелон «а»
            self.outbox.note("detector", detector="human_request")
            await self.do_handoff("client_request")
            return
        # условие №3 (негатив) — классификатор на Haiku; тик не тормозим,
        # реакция уезжает отдельной короткой транзакцией после commit'а.
        self.pending_classification = incoming.strip() or None
        await self.capture_phone(incoming)  # эшелон 1 извлечения (02 §3.5)

        # --- подсказка в идущем разговоре (21.08) ---
        # Никаких приветствий, счётчиков «мимо сценария» и bot_active: подсказка —
        # это заметка сотруднику, а не участие бота в диалоге. Один входящий —
        # ровно одна подсказка, дальше сценарий не разматывается.
        if self.is_suggest_mode:
            шаг = suggest_entry_step(
                self.scenario, fresh=fresh, conv_status=str(getattr(self.conv, "status", "") or "")
            )
            if шаг:
                s.waiting = None
                s.next_step = шаг
                self._suggest_once = True
                return

        # --- продолжение сценария ---
        if fresh:
            await self._start_scenario()
            return

        waiting = s.waiting
        if waiting is None:
            # ⚠ ЖДАТЬ НЕЧЕГО, НО «МИМО СЦЕНАРИЯ» ЭТО НЕ ВСЕГДА (проверка 24.09).
            # Условие №4 (02 §4) — про клиента, который пишет, когда бот своё уже
            # сказал. Два состояния выглядят так же, но не про это, и считать их
            # мимо сценария значило молчать: ИИ не вызывался, а на третьем
            # сообщении шла передача с ложной причиной.
            current = self.scenario.step(s.step) if s.step else None
            if current is not None and current.type == "ai_answer":
                # Ответ этого шага не состоялся: отброшен маркером свежести или
                # остался подсказкой. Шаг ИИ отвечает заново, по полной переписке.
                self.outbox.note("resume", step=current.id)
                s.next_step = current.id
                return
            if not self.is_suggest_mode and not await self._client_heard_bot():
                # Сценарий шёл подсказкой, а бота переключили на «Отвечать
                # клиенту»: клиент не видел ни приветствия, ни вопроса. Начинаем
                # сначала — для него разговор с ботом только начинается.
                self.outbox.note("restart_after_suggest")
                await self._start_scenario()
                return
            # бот ничего не спрашивал, а клиент пишет — «мимо сценария» (условие №4)
            s.counters.offscript_msgs += 1
            self.outbox.note("offscript", count=s.counters.offscript_msgs)
            if s.counters.offscript_msgs >= self.limits.max_offscript_messages:
                await self.do_handoff("offscript")
            return

        step = self.scenario.step(waiting.step_id or s.step)
        if step is None:  # сценарий отредактировали под ногами (02 §2.1)
            await self.do_handoff("scenario_changed")
            return
        s.step = step.id

        if waiting.kind == "menu":
            await self._answer_menu(step, waiting, incoming)
        else:
            await self._answer_ask(step, waiting, incoming)

    async def _start_scenario(self) -> None:
        """Сценарий с начала: бот берёт диалог и идёт со стартового шага."""
        # ⚠ ПОДСКАЗКА ДИАЛОГ НЕ ВЕДЁТ (проверка 24.09): `bot_active` прячет его
        # из «Входящих», а клиенту подсказка не пишет ничего — новый диалог
        # пропадал у всех на 10–12 минут, до сторожа зависших.
        if not self.is_suggest_mode:
            self.conv.bot_active = True
            # кадр всем операторам: без него строка-призрак висела во
            # «Входящих» до ручного обновления (аудит 16.08)
            self.outbox.event(
                "conversation:updated",
                conversation_updated_event(self.conv, {"bot_active": True}),
            )
        if not self.scenario.has(self.scenario.entry):
            log.warning("bot.entry_missing", bot_id=str(self.bot_id))
            await self.do_handoff("scenario_changed")
            return
        self.state.next_step = self.scenario.entry

    async def _client_heard_bot(self) -> bool:
        """Писал ли бот клиенту в этом диалоге хоть раз. Подсказки — заметки
        (`direction='note'`) и сюда не входят."""
        return bool(
            await self.db.scalar(
                select(
                    exists().where(
                        Message.conversation_id == self.conv.id,
                        Message.direction == "out",
                        Message.sender_type == "bot",
                    )
                )
            )
        )

    async def _answer_ask(self, step: Step, waiting: Any, text: str) -> None:
        ok, value = validate_answer(step.params, text)
        if not ok:
            await self.handle_invalid_answer(step, waiting, default_reason="ask_invalid")
            return
        var = step.params.get("var")
        if isinstance(var, str) and var:
            self.state.vars[var] = value
        if step.params.get("validate") == "phone" and value:
            await self.capture_phone(value)
        self.state.stop_waiting()
        self.state.counters.offscript_msgs = 0
        self.outbox.note("answer", var=var, value=value)
        self._goto(step.next)

    async def _answer_menu(self, step: Step, waiting: Any, text: str) -> None:
        options = step.params.get("options")
        # Последнее сообщение серии — самое свежее решение клиента, затем более
        # ранние, затем серия целиком (ключевое слово могло разойтись по строкам).
        option = next(
            (
                found
                for candidate in (*reversed(self.series), text)
                if (found := match_menu_option(options, candidate)) is not None
            ),
            None,
        )
        if option is not None:
            var = step.params.get("var")
            if isinstance(var, str) and var:
                self.state.vars[var] = option.get("id")
            self.state.stop_waiting()
            self.state.counters.offscript_msgs = 0
            self.outbox.note("answer", var=var, value=option.get("id"))
            self._goto(option.get("next"))
            return
        # несовпадение с меню — тоже «мимо сценария» (условие №4)
        self.state.counters.offscript_msgs += 1
        self.outbox.note("offscript", count=self.state.counters.offscript_msgs)
        if self.state.counters.offscript_msgs >= self.limits.max_offscript_messages:
            # Явная ветка сценария важнее общего правила: она и есть ответ
            # автора на «клиент не попадает в меню».
            target = step.on_no_match or step.on_invalid
            self.state.stop_waiting()
            if target:
                self._goto(target)
            else:
                await self.do_handoff("offscript")
            return
        await self.handle_invalid_answer(step, waiting, default_reason="offscript")

    async def handle_invalid_answer(self, step: Step, waiting: Any, *, default_reason: str) -> None:
        """Ретрай невалидного ответа (02 §2.4).

        Дедлайн и токен НЕ пересоздаём: у шага один общий дедлайн, иначе
        «неправильный ответ» бесконечно продлевал бы ожидание.
        """
        waiting.attempts += 1
        self.state.counters.attempts += 1
        max_attempts = step.params.get("max_attempts")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            max_attempts = 2
        if waiting.attempts >= max_attempts:
            self.state.stop_waiting()
            target = step.on_invalid or step.on_no_match
            if target:
                self._goto(target)
            else:
                await self.do_handoff(default_reason)
            return
        retry_text = step.params.get("retry_text")
        if retry_text:
            rendered = await self.render_full(retry_text)
            if rendered.text:
                await self.send_bot_message(rendered.text)
            elif rendered.lost_everything:
                await self.report_empty_render(step, rendered)
        self.outbox.note("retry", step=step.id, attempts=waiting.attempts)

    # --------------------------------------------------------------- таймаут

    async def on_timeout(self, token: str | None = None) -> None:
        """Истёк дедлайн `ask`/`menu` (02 §2.4)."""
        s = self.state
        waiting = s.waiting
        if waiting is None:  # клиент уже ответил — задача самоаннулировалась
            self.outbox.note("timeout_ignored")
            return
        if token is not None and waiting.token and waiting.token != token:
            self.outbox.note("timeout_ignored", reason="token_mismatch")
            return
        step = self.scenario.step(waiting.step_id or s.step)
        s.stop_waiting()
        if step is None:
            await self.do_handoff("scenario_changed")
            return
        s.step = step.id
        self.outbox.note("timeout", step=step.id)
        # ⚠ В РЕЖИМЕ ПОДСКАЗКИ ДОЖИМА НЕТ (26.08, вторая жалоба владельца на один и тот
        # же текст). Дожим — это сообщение молчащему клиенту. В режиме подсказки боту
        # клиент не отвечает и не может ответить: всё, что бот «сказал», лежит заметкой,
        # которую видит только смена. Значит `ask` ждёт ответа на вопрос, которого клиент
        # не получал, а по таймауту в ленту падает «Ну что, подскажете?» — обращение к
        # человеку, у которого ничего не спрашивали. Оператору это шум, владельцу — бот,
        # который «пишет ерунду». Ветку таймаута ведём в очередь: решает человек.
        if self.is_suggest_mode:
            self.outbox.note("timeout_handoff_suggest", step=step.id)
            await self.do_handoff("ask_timeout")
        elif step.on_timeout:
            self._goto(step.on_timeout)
        else:  # дефолт: молча передать в очередь
            await self.do_handoff("ask_timeout")

    # ------------------------------------------------------------ основной цикл

    def resume_ai(self, answer: AiAnswer) -> None:
        """Продолжить шаг ИИ во второй транзакции тика с готовым ответом модели."""
        request = answer.request
        self.prefetched_ai = answer
        self.last_incoming = request.last_incoming
        self.series = list(request.series)
        self.pending_classification = request.pending_classification
        if request.suggest_once:
            self._suggest_once = True
        self.state.next_step = request.step_id

    def _goto(self, target: str | None) -> None:
        """Перейти на шаг. Битая/отсутствующая ссылка — не «тихо стоим», а
        аккуратный handoff: диалог не должен зависнуть у бота."""
        if target and self.scenario.has(target):
            self.state.next_step = target
            return
        log.warning(
            "bot.broken_ref",
            bot_id=str(self.bot_id),
            step=self.state.step,
            target=target,
        )
        self.state.next_step = None
        self._broken_ref = True  # обработает run(); handoff требует await

    async def run(self) -> None:
        """Основной цикл: до `ask`/терминала/лимита (02 §2.3)."""
        s = self.state
        if self._broken_ref:  # ссылка сломалась ещё в on_incoming/on_timeout
            self._broken_ref = False
            await self.do_handoff("scenario_changed")
            return
        executed = 0
        while s.next_step is not None:
            if (
                s.counters.steps_total >= self.limits.max_steps_total
                or executed >= self.limits.max_steps_per_tick
                or s.counters.bot_msgs_row >= self.limits.max_bot_messages_row
            ):
                log.error(  # 02 §2.7: всегда ошибка конфигурации сценария
                    "bot.loop_protection",
                    bot_id=str(self.bot_id),
                    conversation_id=str(self.conv.id),
                    step=s.step,
                    counters=s.counters.dump(),
                    executed=executed,
                )
                await self.do_handoff("loop_protection")
                return
            step = self.scenario.step(s.next_step)
            if step is None:  # сценарий отредактировали под ногами
                await self.do_handoff("scenario_changed")
                return
            s.step, s.next_step = step.id, None
            s.counters.steps_total += 1
            executed += 1
            s.touch(self.now())
            self.outbox.note("step", id=step.id, type=step.type)
            await self.execute(step)
            if getattr(self, "_suggest_once", False):
                # одна подсказка на одно входящее: дальше по сценарию не идём,
                # иначе на чужой диалог посыпались бы send/ask-заметки подряд
                self._suggest_once = False
                s.next_step = None
                return
            if self._broken_ref:
                self._broken_ref = False
                await self.do_handoff("scenario_changed")
                return

    async def execute(self, step: Step) -> None:
        handler = {
            "send": self.exec_send,
            "ask": self.exec_ask,
            "menu": self.exec_menu,
            "condition": self.exec_condition,
            "ai_answer": self.exec_ai_answer,
            "handoff": self.exec_handoff,
            "close": self.exec_close,
            "tag": self.exec_tag,
            "note": self.exec_note,
        }[step.type]
        await handler(step)

    # ----------------------------------------------------------------- шаги

    def _текст_шага(self, step: Step) -> Any:
        """Текст шага. Список — это ПУЛ формулировок, из него берётся одна.

        ⚠ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 26.08: «формулировка должна быть уникальной всегда и
        подходить к контексту». Одна зашитая строка на всех клиентов — гарантированный
        повтор: дожим молчания получали слово в слово все, кто замолчал. Это тот же
        разбор, что и у зерна вариативности в лид-боте днём: уникальность держится не
        текстом, а выбором из пула по САМОМУ ДИАЛОГУ.

        Выбор стабилен внутри диалога и шага: повторный проход того же шага не должен
        выглядеть как сбой («он мне уже это писал, но другими словами»). Между разными
        диалогами формулировки расходятся — это и есть уникальность.

        ⚠ ЧЕГО ЭТОТ ПРИЁМ НЕ ДАЁТ, СКАЗАНО ЧЕСТНО: пул подбирает формулировку под ШАГ,
        а не под конкретную реплику клиента. Шаг сам по себе и есть контекст («клиент
        не описал поломку» — все варианты спрашивают о ней), но дословной подстройки
        под чужой текст без вызова модели тут не будет.
        """
        сырой = step.params.get("text")
        if not isinstance(сырой, list):
            return сырой
        варианты = [т for т in сырой if isinstance(т, str) and т.strip()]
        if not варианты:
            return None
        зерно = hashlib.sha256(f"{getattr(self.conv, 'id', '')}|{step.id}".encode()).hexdigest()
        return варианты[int(зерно, 16) % len(варианты)]

    async def exec_send(self, step: Step) -> None:
        rendered = await self.render_full(self._текст_шага(step))
        if rendered.text:
            await self.send_bot_message(rendered.text)
        elif rendered.lost_everything:
            await self.report_empty_render(step, rendered)
        self._goto(step.next)

    async def report_empty_render(self, step: Step, rendered: Rendered) -> None:
        """Сообщение шага целиком съела пустая подстановка (02 §1.2).

        Молчать здесь нельзя: клиент не получил реплику, а сценарий поехал
        дальше — со стороны это «бот пропустил ход». Пишем ЗАМЕТКУ (её видят
        только сотрудники) с именем шага и пустыми плейсхолдерами, чтобы
        починка сводилась к «открыть шаг и поправить текст», а не к чтению
        логов.
        """
        placeholders = list(rendered.dropped)
        log.error(
            "bot.empty_render",
            bot_id=str(self.bot_id),
            conversation_id=str(self.conv.id),
            step=step.id,
            placeholders=placeholders,
        )
        self.outbox.note("empty_render", step=step.id, placeholders=placeholders)
        await self.add_note(
            EMPTY_RENDER_NOTE.format(step=step.id, placeholders=", ".join(placeholders))
        )

    async def _begin_waiting(self, step: Step, kind: str) -> None:
        if self.is_suggest_mode:
            # ⚠ ПОДСКАЗКА ДИАЛОГ НЕ ВЕДЁТ (проверка 24.09). Ожидание ставило
            # `bot_active` и дедлайн: диалог прятался из очереди, а по сроку
            # приходила передача «клиент не ответил» на вопрос, который клиент
            # видел разве что со слов оператора.
            if suggest_entry_step(self.scenario, fresh=False, conv_status="") is not None:
                # Есть шаг ИИ: следующее сообщение клиента даст подсказку по
                # контексту — ждать нечего.
                self.outbox.note("suggest_no_wait", step=step.id)
                return
            # Сценарий без ИИ — сценарий-подсказчик для оператора: запоминаем,
            # о чём спросили, чтобы ответ клиента попал в меню или вопрос, — но
            # без срока и без `bot_active`: торопить и прятать тут некого.
            var = step.params.get("var")
            self.state.begin_waiting(
                kind=kind,
                step_id=step.id,
                var=var if isinstance(var, str) else None,
                timeout=None,
                now=self.now(),
            )
            self.outbox.note("suggest_wait", step=step.id)
            return
        params = step.params
        # ⚠ ДЕФОЛТ ОЖИДАНИЯ — НЕ СУТКИ (28.08). Шаг без явного `timeout` ждал 24 часа, и
        # боевой сценарий лид-бота именно такой: `wait_client` таймаута не задаёт, значит
        # напоминание молчащему клиенту уходило через сутки, а передача человеку — через
        # двое. К этому времени клиента давно нет.
        # Два независимых замера сходятся на десятках минут, а не на сутках:
        #   · живые дожимы (27 786 диалогов, 3202 напоминания): p25 = 11 мин, медиана
        #     25 мин, p75 = 46 мин — записано в test_default_scenario_ping;
        #   · отдача напоминания (5 269 случаев, архив Jivo): через 1–15 мин клиент
        #     отвечает в 43,1 %, через 15–60 мин — 27,6 %, через 1–4 ч — 18,5 %,
        #     через 4–12 ч — 12,2 %.
        # Берём медиану живых: 25 минут. Быстрее нельзя — регламент Авито связывает
        # настырность с жалобами и блокировкой аккаунта (то же обоснование в тесте пинга).
        # Шаг, которому нужны сутки, задаёт их явно; молчаливый дефолт больше не решает
        # за сценарий.
        timeout = parse_timeout(params.get("timeout", "25m"))
        var = params.get("var")
        waiting = self.state.begin_waiting(
            kind=kind,
            step_id=step.id,
            var=var if isinstance(var, str) else None,
            timeout=timeout,
            now=self.now(),
        )
        if not self.conv.bot_active:
            self.conv.bot_active = True
            self.outbox.event(
                "conversation:updated",
                conversation_updated_event(self.conv, {"bot_active": True}),
            )
        if timeout:
            # Отложенная задача сверит токен и умрёт, если клиент уже ответил.
            self.outbox.job(
                "bot_ask_timeout",
                self.conv.id,
                waiting.token,
                defer_by=timeout,
                job_id=f"bot_timeout:{self.conv.id}:{waiting.token}",
            )
        self.outbox.note("waiting", waiting_kind=kind, var=waiting.var, deadline=waiting.deadline)
        # next_step не выставляем -> run() останавливается, бот в WAITING

    async def exec_ask(self, step: Step) -> None:
        if not await self.ask_question(step):
            return
        await self._begin_waiting(step, "ask")

    async def exec_menu(self, step: Step) -> None:
        if not await self.ask_question(step):
            return
        await self._begin_waiting(step, "menu")

    async def ask_question(self, step: Step) -> bool:
        """Задать вопрос шага. `False` — вопроса не вышло, ждать нечего.

        Пустой `params.text` — штатный случай (в «Первичном приёме» вопрос
        задаёт предыдущий шаг `send`), и он возвращает True.

        А вот текст, который ЦЕЛИКОМ съела пустая подстановка, — беда: раньше
        бот всё равно вставал в ожидание, и диалог молча висел до конца
        таймаута (сутки по умолчанию), потому что клиент не видел ни одного
        вопроса. Такой шаг отдаём человеку сразу и объясняем в заметке, что
        именно оказалось пустым.
        """
        template = step.params.get("text")
        if not template:
            return True
        rendered = await self.render_full(template)
        if rendered.text:
            await self.send_bot_message(rendered.text)
            return True
        if not rendered.lost_everything:  # текст из одних пробелов — не наш случай
            return True
        placeholders = list(rendered.dropped)
        log.error(
            "bot.empty_question",
            bot_id=str(self.bot_id),
            conversation_id=str(self.conv.id),
            step=step.id,
            placeholders=placeholders,
        )
        self.outbox.note("empty_render", step=step.id, placeholders=placeholders, question=True)
        await self.do_handoff(
            "scenario",
            comment=EMPTY_QUESTION_COMMENT.format(
                step=step.id, placeholders=", ".join(placeholders)
            ),
        )
        return False

    async def exec_condition(self, step: Step) -> None:
        target = pick_condition_branch(
            step,
            values=await self.context_values(),
            last_text=self.last_incoming,
            now=self.now(),
        )
        self.outbox.note("condition", step=step.id, next=target)
        self._goto(target)

    async def _record_leadbot(self, outcome: str) -> None:
        """След обращения к лид-боту в журнал работы (`leadbot_calls`).

        ЗОВЁТСЯ ИЗ КАЖДОГО ВЫХОДА `exec_ai_answer`, И ЭТО НАМЕРЕННО. Соблазн —
        записать один раз в конце; но выходов четыре, и различаются они ровно
        тем, ради чего журнал заводится: «ответ ушёл клиенту», «лёг подсказкой»,
        «ответил и передал человеку», «ответ был, но выброшен за уверенность».
        Свести их в одну запись «обратились и получили 200» значит потерять
        единственный интересный вопрос — что из этого увидел клиент.

        МОЛЧИТ ДЛЯ ЧУЖОГО ПОСТАВЩИКА. У Claude нет ни слоёв, ни эскалации с
        причиной, ни цены вызова на той стороне — журнал лид-бота о нём соврал бы
        пустыми полями. Его работа видна в обычных логах и в ленте диалога.
        """
        if provider_of(self.bot) != LEADBOT:
            return
        reply = leadbot.last_call.get()
        if reply is None:  # шаг до сети не дошёл (пустой диалог)
            return
        leadbot.last_call.set(None)
        # Вопрос клиента — последняя его реплика, та самая, на которую отвечали.
        # Берём из уже загруженного диалога, а не отдельным запросом: сессия
        # внутри тика держит транзакцию, и лишний поход в базу здесь не нужен.
        question = None
        for line in reversed(self._last_ai_dialog or []):
            if line.get("role") == "user":
                question = line.get("content")
                break
        # лид-боту диалог уходит СЫРЫМ (mask=False с 16.08) — а в журнал телефон
        # клиента попадать не должен, маскируем при записи
        if isinstance(question, str):
            question = mask_phones(question)
        await leadbot_log.record(
            self.db,
            reply,
            outcome=outcome,
            conversation_id=getattr(self.conv, "id", None),
            account_id=getattr(self.conv, "account_id", None),
            question=question if isinstance(question, str) else None,
            now=self.now(),
        )

    async def exec_ai_answer(self, step: Step) -> None:
        params = step.params
        threshold = _as_float(params.get("confidence_threshold"), 0.6)
        max_reply_len = _as_int(params.get("max_reply_len"), 800)
        context_messages = _as_int(params.get("context_messages"), 10)

        prefetched = self.prefetched_ai
        if prefetched is not None and prefetched.request.step_id == step.id:
            # Вторая транзакция тика: модель ответила без блокировки строки
            # диалога, свежесть ответа сверил тик (`runtime`, проверка 24.09).
            self.prefetched_ai = None
            self._last_ai_dialog = prefetched.request.dialog
            self.state.counters.ai_calls += 1
            result = prefetched.result
        else:
            # Третий шаг пути: бот пошёл за ответом. Между этой отметкой и
            # следующей лежит вся задержка чужого сервиса — по ним и видно,
            # он ли тормозит (`app/core/trace.py`).
            trace.step(
                "trace.bot_asks",
                conversation_id=str(getattr(self.conv, "id", "")),
                provider=provider_of(self.bot),
                context_messages=context_messages,
            )
            if self.defer_ai:
                try:
                    request = await self._ai_request(step.id, context_messages)
                except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
                    log.warning("bot.ai_answer_failed", error=type(exc).__name__)
                    request = None
                if request is not None:
                    # Модель ответит после commit'а: шаг продолжит вторая
                    # транзакция тика, сценарий здесь останавливается. Счёт шага
                    # тоже за ней — иначе каждый ответ ИИ считался бы дважды, и
                    # защита от зацикливания (02 §2.7) срабатывала бы вдвое раньше.
                    self.state.counters.steps_total -= 1
                    self.deferred_ai = request
                    self.state.next_step = None
                    return
                self.state.counters.ai_calls += 1
                result = None
            else:
                self.state.counters.ai_calls += 1
                # ⚠ МАРКЕР СВЕЖЕСТИ (вопрос владельца 30.08: «можно ли, чтобы бот в моменте
                # менял свой ответ?»). Пока модель думает (секунды), клиент успевает дописать
                # — на скрине владельца он спросил цену и тут же дал адрес. Ответ, сочинённый
                # по неполному контексту, отправлять нельзя: запоминаем последнее входящее ДО
                # вызова, сверяем ПОСЛЕ. Появилось новое — этот ответ отбрасывается молча:
                # свежий тик от нового сообщения уже в очереди и ответит по полной картине.
                _вход_посл = await self._latest_incoming_id()
                result = await self._call_ai_answer(context_messages)
                _выход_посл = await self._latest_incoming_id()
                if result is not None and _выход_посл != _вход_посл:
                    log.info(
                        "bot.reply_stale_dropped",
                        conversation_id=str(self.conv.id),
                        reason="client_wrote_while_thinking",
                    )
                    self.outbox.note("ai_call_stale")
                    return  # шаг не двигаем: свежий тик пройдёт сценарий заново с полным контекстом
        if result is None:  # 02 §3.3: таймаут/недоступность — НИКОГДА не ретраим
            # ⚠ ЗАМЕТКУ ПИШЕМ ОДИН РАЗ НА ДИАЛОГ (28.08). Передача защищена
            # `handoff_done`, а заметка — нет, и в ленту падало «AI недоступен» на
            # каждую попытку. По боевой базе: 14 диалогов с повторными записями, до
            # шести штук в одном; всего 29 лишних. Оператору это шум ровно там, где
            # он и так остался без помощи бота.
            if not self.state.handoff_done():
                await self.add_note(AI_UNAVAILABLE_NOTE)
            await self._record_leadbot(LB_UNAVAILABLE)
            await self.do_handoff("ai_unavailable")
            return

        confidence = _as_float(result.get("confidence"), 0.0)
        needs_operator = bool(result.get("needs_operator"))
        reply = str(result.get("reply") or "")
        self.state.vars["_ai_confidence"] = confidence
        self.outbox.note("ai_call", confidence=confidence, needs_operator=needs_operator)

        # ⚠ ДВА НЕОБЯЗАТЕЛЬНЫХ ПОЛЯ СВЕРХ КОНТРАКТА (12 августа).
        #
        # Бэкенд может знать про диалог больше, чем помещается в «текст + нужен ли
        # человек». Лид-бот, например, отдаёт ПРИЧИНУ передачи («вопрос о статусе
        # визита») и срок реакции в минутах, а ещё замечает собранную заявку —
        # телефон есть, окно предложено. Раньше это уходило только в лог сервера,
        # то есть человеку, который сидит в диалоге, было недоступно: он видел
        # «бот передал», без единого слова о том, почему и насколько это срочно.
        #
        # `note` — строка в ленту диалога (её видит только сотрудник).
        # `handoff_comment` — та же мысль, но внутри сводки передачи, чтобы не
        # плодить вторую заметку об одном событии.
        # Оба поля необязательные: бэкенд без них работает ровно как раньше.
        note = str(result.get("note") or "").strip()
        if note:
            await self.add_note(note)

        if needs_operator or confidence < threshold:
            if reply and needs_operator:
                # фраза-мост «передаю мастеру» (02 §3.2); содержательного
                # ответа при низкой уверенности клиент не получает
                await self.send_bot_message(reply[:max_reply_len])
                await self._record_leadbot(LB_BRIDGE)
            elif needs_operator:
                await self._record_leadbot(LB_HANDOFF)
            else:
                # Уверенность ниже порога: ответ БЫЛ, и он мог быть хорошим, но
                # клиент не получил ничего. Для него это молчание — и в журнале
                # это обязано отличаться от «ответили».
                #
                # ⚠ В РЕЖИМЕ ПОДСКАЗКИ СЧЁТ ДРУГОЙ (21.08). Клиенту и так не уходит
                # ничего, значит выброшенным черновиком мы наказываем не его, а
                # диспетчера: он остаётся без помощи ровно там, где бот растерялся.
                # По живой выгрузке это 64 подсказки из 924 (6.9%) — каждая
                # четырнадцатая. Черновик кладём заметкой, передачу человеку не
                # отменяем: причина видна в ленте, решает человек.
                if reply and self.is_suggest_mode:
                    await self.send_bot_message(reply[:max_reply_len])
                await self._record_leadbot(LB_LOW_CONFIDENCE)
            if step.on_low_confidence:
                self._goto(step.on_low_confidence)
            else:
                comment = str(result.get("handoff_comment") or "").strip() or None
                # ⚠ ПРИЧИНУ НАЗЫВАЕТ ТОТ, КТО ЗОВЁТ ЧЕЛОВЕКА (26.08). Раньше здесь стоял
                # глухой «ai_low_confidence» на все случаи разом, и очередь получала «AI
                # не уверен в ответе» даже там, где бэкенд точно знал причину и написал
                # её рядом заметкой: «клиент звонил», «тема по согласованию», «вопрос о
                # статусе визита». Оператор сортирует очередь по причине передачи —
                # значит именно она обязана быть правдой, а не заглушкой.
                # ⚠ НИЗКАЯ УВЕРЕННОСТЬ ОСТАЁТСЯ САМА СОБОЙ: если бэкенд причины не дал
                # ИЛИ дело действительно в пороге (ответ был, но не прошёл), пишем её.
                причина = "ai_low_confidence"
                if needs_operator:
                    от_бэкенда = (result.get("meta") or {}).get("escalation") or {}
                    названа = str(от_бэкенда.get("reason") or "").strip()
                    if названа:
                        причина = названа
                await self.do_handoff(причина, comment=comment, render_comment=False)
            return

        # ⚠ БЭКЕНД МОЖЕТ МОЛЧАТЬ ОСОЗНАННО, И ЭТО НЕ «ОТВЕТ УШЁЛ» (27.08). На вежливое «Ок»
        # лид-бот отвечает пустотой с `needs_operator=false`: сказать нечего, а диалог он
        # держит за собой — панель второй раз бота не пустит, и уйди диалог человеку, вместе
        # с ним ушла бы заявка, которую бот собрал бы, когда клиент напишет «я дома».
        # `send_bot_message` пустой текст не отправляет и возвращает None. Записать при этом
        # «Ответ ушёл клиенту» значит соврать в журнале, по которому владелец считает работу.
        отправлено = await self.send_bot_message(reply[:max_reply_len])
        if отправлено is None:
            # ⚠ `None` ПРИХОДИТ ПО ДВУМ РАЗНЫМ ПОВОДАМ, И РАЗЛИЧАЛСЯ ТОЛЬКО ОДИН.
            # Пустой текст — тихое удержание, законный ход. Но `None` возвращает
            # и гард повторов: он гасит ответ, дословно совпавший с недавним, и
            # клиент не получает НИЧЕГО. Условие ниже проверяло только пустоту,
            # поэтому на погашенном повторе выполнение шло дальше и писало в
            # журнал «ответ ушёл клиенту» — при том что в базе нет ни сообщения,
            # ни заметки. Правило записано строкой выше этим же комментарием:
            # журнал обязан называть исход, который случился на самом деле.
            if not reply.strip():
                log.info("bot.silent_hold", conversation_id=str(getattr(self.conv, "id", "")))
            else:
                log.info(
                    "bot.reply_suppressed",
                    conversation_id=str(getattr(self.conv, "id", "")),
                    reason="repeat",
                )
            self._goto(step.next)
            return
        # Режим решает, кто увидел текст: «подсказка» — только оператор,
        # «автоответ» — клиент. Перехват стоит в `send_bot_message`, и журнал
        # обязан называть тот же исход, что случился на самом деле.
        await self._record_leadbot(LB_HINT if self.is_suggest_mode else LB_SENT)
        # ⚠ ПОСЛЕ ОКОНЧАТЕЛЬНОГО ОТКАЗА СЦЕНАРИЙ НЕ ПРОДОЛЖАЕТСЯ (снимок владельца 26.08).
        # Клиент спросил про замену матрицы, бот ответил «с битой матрицей не помогу» — и
        # через пять минут сам же дожал «Ну что, расскажете, что случилось?»: шаг ушёл на
        # `next`, тот оказался вопросом, и его таймаут отработал как обычно. Владелец:
        # «зачем бот ведёт дальше диалог, когда понятно, что тут матрица».
        # Признак берём у бэкенда: `meta.flag.kind == "refuse"` он ставит ТОЛЬКО на
        # окончательных отказах (согласование и удержание приходят с needs_operator и
        # разбираются веткой выше). Диалог уходит людям: клиент после отказа часто пишет
        # ещё — в этом самом диалоге он прислал фото разбитого экрана, — и смотреть на
        # это должен человек, а не бот, которому сказать больше нечего.
        _мета = result.get("meta") or {}
        if str(((_мета.get("flag")) or {}).get("kind") or "") == "refuse":
            # ⚠ 29.08 ЗАКРЫВАЕМ, А НЕ ОТДАЁМ ЧЕЛОВЕКУ (решение владельца). Прежний
            # довод — «клиент после отказа часто пишет ещё» — остаётся верным, но
            # он не требует держать диалог ОТКРЫТЫМ: напишет — переоткроется сам и
            # попадёт человеку (`mute` в `close_by_leadbot` не пустит бота на
            # второй круг). До правки такие диалоги копились в очереди живыми,
            # хотя говорить по ним было уже не о чем.
            await self.close_by_leadbot(
                reason="refused",
                outcome="not_our_profile",
                note="🤖 Клиент отказался или работа не наша — диалог закрыт ботом. "
                "Напишет ещё — вернётся в очередь.",
            )
            return
        # ⚠ ЗАЯВКА СОБРАНА — ГОВОРИТЬ БОЛЬШЕ НЕ О ЧЕМ (решение владельца 29.08).
        # Телефон, адрес и время у бота есть, карточка в CRM создана. Раньше здесь
        # оставалась только заметка «подтвердите время», а диалог висел в очереди:
        # по замеру корпуса девять таких диалогов из десяти дальше не продолжаются,
        # а тот десятый вернётся сам — закрытие в LeadChat переоткрываемо.
        # ⚠ КЛИЕНТ ЗАВЕРШИЛ САМ («мне выезд не подходит», «уже вызвала мастера») —
        # бот попрощался, говорить больше не о чем. Итог «отказался», не «непрофиль»:
        # работа наша, отказался клиент. Напишет ещё — диалог переоткроется человеку.
        if _мета.get("client_closed"):
            await self.close_by_leadbot(
                reason="client_closed",
                outcome="declined",
                note="🤖 Клиент завершил разговор — диалог закрыт ботом. "
                "Напишет ещё — вернётся в очередь.",
            )
            return
        if _мета.get("lead_ready"):
            await self.close_by_leadbot(
                reason="lead_ready",
                outcome="visit",
                note="🤖 Заявка собрана и передана — диалог закрыт ботом. "
                "Напишет ещё (перенос, вопрос) — вернётся в очередь.",
            )
            return
        self._goto(step.next)

    async def exec_handoff(self, step: Step) -> None:
        params = step.params
        reason = params.get("reason")
        tags = params.get("tags")
        await self.do_handoff(
            reason if isinstance(reason, str) and reason else "scenario",
            comment=params.get("comment"),
            tags=tags if isinstance(tags, list) else (),
        )

    async def exec_close(self, step: Step) -> None:
        # ⚠ ЧУЖОЙ ДИАЛОГ И ПОДСКАЗКА НЕ ЗАКРЫВАЮТ (проверка 24.09) — тот же замок,
        # что у `close_by_leadbot` с 30.08. Ветка сценария доходила до close по
        # таймауту или пункту меню, когда диалог уже назначили оператору, — и он
        # исчезал из «Моих»; в режиме подсказки «подсказка» закрывала диалог
        # по-настоящему. Остаётся заметка — польза подсказки.
        if self.is_suggest_mode or self.conv.assignee_id or self.conv.claimed_by_id:
            log.info(
                "bot.close_skipped_foreign",
                conversation_id=str(getattr(self.conv, "id", "")),
                step=step.id,
                suggest=self.is_suggest_mode,
                has_owner=bool(self.conv.assignee_id or self.conv.claimed_by_id),
            )
            await self.add_note(CLOSE_SKIPPED_NOTE.format(step=step.id))
            self.state.stop_waiting()
            self.state.next_step = None
            return
        params = step.params
        text = params.get("text")
        if text and not params.get("silent"):
            rendered = await self.render_full(text)
            if rendered.text:
                await self.send_bot_message(rendered.text)
            elif rendered.lost_everything:
                await self.report_empty_render(step, rendered)
        previous = self.conv.status
        status_dict.set_status(self.conv, "closed", now=self.now())
        # Закрытый диалог будить некому: отложка снимается вместе со статусом,
        # иначе сторож поднял бы закрытое обращение через сутки.
        status_dict.clear_snooze(self.conv)
        await clear_closed_marks(self.db, self.conv)
        self.conv.bot_active = False
        self.conv.updated_at = self.now()
        await self._apply_close_outcome(step)
        await self.add_note(CLOSE_NOTE.format(step=step.id))
        await self._audit_bot_closed(reason="scenario", step=step.id)
        if previous != "closed":  # контракт журнала 06 §0.3
            await write_audit(
                self.db,
                user_id=None,
                action="conversation.status_changed",
                entity="conversation",
                entity_id=str(self.conv.id),
                details={
                    "from": previous,
                    "to": "closed",
                    "by": "bot",
                    "assignee_id": (str(self.conv.assignee_id) if self.conv.assignee_id else None),
                },
            )
        # Позицию сбрасываем до чистого листа: вернувшийся клиент переоткроет
        # диалог (DESIGN §8.3), и сценарий должен начаться заново (02 §1.3).
        self.state.finish_closed(step.id, self.now())
        self.outbox.event(
            "conversation:updated",
            conversation_updated_event(self.conv, {"status": "closed", "bot_active": False}),
        )
        self.outbox.note("close", step=step.id)

    async def close_by_leadbot(self, *, reason: str, outcome: str, note: str) -> None:
        """Закрыть диалог по решению лид-бота: заявка собрана или клиент отказал.

        ⚠ РЕШЕНИЕ ВЛАДЕЛЬЦА 29.08. Раньше бот не закрывал ни в одном из этих
        случаев: собранная заявка оставляла диалог висеть с заметкой
        «подтвердите время», отказ уходил человеку. Очередь наполнялась
        обращениями, по которым говорить уже не о чем. Замер корпуса: после
        «записал вас» девять диалогов из десяти дальше не продолжаются.

        ⚠ ПОЧЕМУ ЗДЕСЬ `mute`, А НЕ ПРОСТО ЗАКРЫТИЕ. Закрытие в LeadChat не
        окончательно: напишет клиент — диалог переоткроется (DESIGN §8.3), и
        это как раз то, что делает автозакрытие безопасным. Но `finish_closed`
        сбрасывает позицию сценария «до чистого листа», и без `mute` бот вошёл
        бы ЗАНОВО: клиент пишет «а можно перенести на завтра?» — а бот
        здоровается и спрашивает, что случилось. `muted` переживает
        `finish_closed` (см. его докстринг) и оставляет вернувшегося клиента
        человеку, а не второму кругу сценария.
        """
        # ⚠ ЧУЖОЙ ДИАЛОГ БОТ НЕ ЗАКРЫВАЕТ (аудит 30.08).
        #
        # Решение владельца 29.08 принималось про АВТО-режим, где диалог ведёт
        # бот и больше никто. Но подсказки работают и в операторских диалогах, и
        # тогда эта же ветка выполнялась: оператор ведёт клиента, тот присылает
        # телефон и адрес, лид-бот честно отдаёт `lead_ready` — и диалог
        # ЗАКРЫВАЛСЯ у оператора под руками, с исходом «visit», из которого
        # родилась бы вторая автозаявка рядом с той, что заводит человек.
        # Напишет клиент ещё раз — диалог переоткроется НИЧЬИМ в общей очереди
        # (`inbound` снимает ответственного), и оператор молча теряет клиента.
        #
        # Признаков два, и оба нужны. Режим подсказки — потому что подсказка по
        # определению ничего не решает за человека, она лишь предлагает текст.
        # Наличие хозяина — потому что диалог мог достаться оператору и в
        # авто-режиме (передача, «забрать у бота»), и там правило то же.
        # Заметка при этом остаётся: она и есть польза подсказки.
        if self.is_suggest_mode or self.conv.assignee_id or self.conv.claimed_by_id:
            log.info(
                "bot.close_skipped_foreign",
                conversation_id=str(getattr(self.conv, "id", "")),
                outcome=outcome,
                suggest=self.is_suggest_mode,
                has_owner=bool(self.conv.assignee_id or self.conv.claimed_by_id),
            )
            await self.add_note(note)
            return

        previous = self.conv.status
        status_dict.set_status(self.conv, "closed", now=self.now())
        # Закрытый диалог будить некому — та же причина, что у `exec_close`.
        status_dict.clear_snooze(self.conv)
        await clear_closed_marks(self.db, self.conv)
        self.conv.bot_active = False
        self.conv.updated_at = self.now()
        from app.services.leads import OUTCOMES

        if outcome in OUTCOMES and not self.conv.outcome:
            self.conv.outcome = outcome
            self.conv.outcome_at = self.now()
            await write_audit(
                self.db,
                user_id=None,
                action="conversation.outcome_set",
                entity="conversation",
                entity_id=str(self.conv.id),
                details={"outcome": outcome, "by": "bot", "step": "leadbot"},
            )
        await self.add_note(note)
        if previous != "closed":  # контракт журнала 06 §0.3
            await write_audit(
                self.db,
                user_id=None,
                action="conversation.status_changed",
                entity="conversation",
                entity_id=str(self.conv.id),
                details={
                    "from": previous,
                    "to": "closed",
                    "by": "bot",
                    "assignee_id": (str(self.conv.assignee_id) if self.conv.assignee_id else None),
                },
            )
        await self._audit_bot_closed(reason=reason, step="leadbot")
        self.state.mute()
        self.state.finish_closed("leadbot", self.now())
        self.outbox.event(
            "conversation:updated",
            conversation_updated_event(self.conv, {"status": "closed", "bot_active": False}),
        )
        self.outbox.note("close", step="leadbot")

    async def _audit_bot_closed(self, *, reason: str, step: str) -> None:
        """Своя строка журнала о закрытии ботом (проверка 24.09).

        Экран «Диалоги бота» считал плитку «Бот закрыл сам» по `bot.handoff`,
        а закрытие передачей не является: с 29.08 лид-бот закрывает отказ и
        собранную заявку сам, и плитка всегда показывала 0, а строка — «закрыт
        без передачи», то есть закрытие приписывалось людям.
        """
        await write_audit(
            self.db,
            user_id=None,
            action=BOT_CLOSED_ACTION,
            entity="conversation",
            entity_id=str(self.conv.id),
            details={
                "reason": reason,
                "step": step,
                "bot_id": str(self.bot_id) if self.bot_id else None,
            },
        )

    async def _apply_close_outcome(self, step: Step) -> None:
        """Итог диалога из шага `close` — то, что делает диалог ЗАЯВКОЙ.

        ⚠ ЕДИНСТВЕННЫЙ ПИСАТЕЛЬ `outcome` ПОСЛЕ 12 АВГУСТА. Окно «Чем
        закончилось обращение?» владелец снял, и колонка осталась без
        источника: автозаявки (`leads.py` отбирает `outcome='visit'`) не могли
        родиться ни из чего. Решение владельца от 15 августа, дословно: «авто
        создание нужно только для бота» — итог ставит СЦЕНАРИЙ БОТА, а
        диспетчеры продолжают заводить заявки руками, как заводили.

        Итог сверяется со словарём ДО записи. Валидатор сценария держит тот же
        список, но проверка здесь не дублирование, а последний рубеж: сценарий
        мог приехать в базу мимо валидатора (правка руками, старая ревизия), и
        опечатка превратилась бы в ошибку CHECK-ограничения на живом диалоге —
        закрытие бы упало, клиент остался бы с ботом в вечном цикле.

        Только в пустоту: если итог уже проставлен (диспетчер успел руками),
        бот молчит — человек ближе к разговору, чем сценарий.
        """
        outcome = step.params.get("outcome")
        if not outcome:
            return
        from app.services.leads import OUTCOMES

        if outcome not in OUTCOMES:
            log.warning("bot.close_unknown_outcome", step_id=step.id, outcome=outcome)
            return
        if self.conv.outcome:
            return
        self.conv.outcome = outcome
        self.conv.outcome_at = self.now()
        # `outcome_by_id` остаётся пустым: это колонка «какой СОТРУДНИК решил»,
        # а решил сценарий. Авторство бота едет в журнал строкой ниже.
        await write_audit(
            self.db,
            user_id=None,
            action="conversation.outcome_set",
            entity="conversation",
            entity_id=str(self.conv.id),
            details={"outcome": outcome, "by": "bot", "step": step.id},
        )

    async def exec_tag(self, step: Step) -> None:
        tags = step.params.get("tags")
        added = add_conversation_tags(self.conv, tags if isinstance(tags, list) else [])
        if added:
            self.conv.updated_at = self.now()
            self.outbox.event(
                "conversation:updated",
                conversation_updated_event(self.conv, {"tags": list(self.conv.tags or [])}),
            )
        self.outbox.note("tags", tags=added)
        self._goto(step.next)

    async def exec_note(self, step: Step) -> None:
        await self.add_note(await self.render(step.params.get("text")))
        self._goto(step.next)

    # ------------------------------------------------------------------- AI

    async def load_dialog_for_ai(self, limit: int, *, mask: bool = True) -> list[dict[str, Any]]:
        """Последние сообщения диалога для модели (02 §3.2).

        Только переписка с клиентом (`in`/`out`), заметки и системные записи
        исключены. Роли: клиент -> `user`, бот/оператор -> `assistant`.
        ПРИВАТНОСТЬ: телефоны маскируются `{PHONE}` перед отправкой (запрос
        уходит за границу через шлюз Амстердама, docs/46).

        ⚠ mask=False — для ЛИД-БОТА (16.08): это наш сервис, не внешняя модель,
        и его воронка ищет телефон в репликах, чтобы НЕ просить номер повторно.
        С маской `{PHONE}` бот не видел, что номер уже дан, и переспрашивал —
        сам лид-бот предупреждает об этом в parse_request и шлёт warning.
        """
        rows = list(
            (
                await self.db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == self.conv.id,
                        Message.direction.in_(("in", "out")),
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(max(1, limit))
                )
            ).scalars()
        )
        dialog: list[dict[str, Any]] = []
        for msg in reversed(rows):
            body = (msg.body or "").strip()
            вложения = _attachments_line(getattr(msg, "attachments", None), with_urls=not mask)
            if not body and not вложения:
                continue
            текст = mask_phones(body) if (mask and body) else body
            if вложения:
                текст = (текст + "\n" + вложения).strip() if текст else вложения
            role = "user" if msg.sender_type == "client" else "assistant"
            строка: dict[str, Any] = {"role": role, "content": текст}
            # ⚠ КТО АВТОР НАШЕЙ СТОРОНЫ — БОТ ИЛИ ЖИВОЙ ОПЕРАТОР (26.08). Роль этого не
            # различает: `assistant` стоит и на ответе бота, и на сообщении человека. Лид-боту
            # разница нужна: у него есть заслон «диалог уже ведёт человек, не вмешиваюсь», и
            # без автора он считал СВОИ ЖЕ ответы чужими и замолкал на третьей своей реплике.
            # Снимок владельца: телефон, адрес и время согласованы, а заявки нет — потому что
            # хода не было вовсе. За неделю таких диалогов 517 из 518.
            # Claude лишний ключ не мешает: `claude.ai_answer` собирает свой запрос из
            # `role`/`content` и остального не читает.
            if role == "assistant":
                строка["by"] = "bot" if msg.sender_type == "bot" else "operator"
            dialog.append(строка)
        return dialog

    async def load_bot_context(self, limit: int) -> tuple[list[str], int]:
        """Контекст, которого у бота нет в истории: его прошлые подсказки и звонки клиента.

        ⚠ ЗАЧЕМ ОТДЕЛЬНО ОТ `load_dialog_for_ai` (24.08). Та собирает ПЕРЕПИСКУ —
        только `in`/`out`, и это правильно: она отвечает на вопрос «что клиент
        слышал». Но у бота есть два вопроса, а не один:

          * что клиент слышал — переписка;
          * что я сам уже предлагал — заметки с подсказками.

        В режиме подсказки ответ бота уходит в `add_note`, то есть в историю к нему
        не возвращается вовсе. Из-за этого все его анти-повторные правила считали по
        репликам ОПЕРАТОРА: он трижды предлагал «и номер ваш подскажите» при
        собственном правиле «максимум две» и трижды подряд повторял одну фразу.

        Подмешать подсказки в переписку НЕЛЬЗЯ: клиент их не видел, оператор мог их
        проигнорировать и написать своё. Бот решит, что уже поздоровался, и срежет
        приветствие, которого клиент не получал. Поэтому — отдельным полем.

        ЗВОНКИ. Клиент, набравший номер трижды, просит звонка яснее, чем словами.
        Событие лежит служебной записью (`direction='system'`), а в переписку она не
        входит по построению — бот о звонке не знал никогда.

        ⚠ СЧИТАЕМ ТОЛЬКО ЗАПИСИ АВИТО, А НЕ ЛЮБЫЕ СЛУЖЕБНЫЕ (28.08). Корень
        «звонил» стоит и в НАШЕМ словаре причин передачи: `call_made` —
        «клиент звонил, о чём говорили, знает только оператор» (handoff.py).
        Передав диалог по этой причине, бот писал в ленту свою же строку с этим
        словом и на следующем тике считал её ТРЕТЬИМ звонком клиента: два
        настоящих плюс собственная запись. Диалог уходил с ярлыком «клиент
        просит позвонить», и диспетчер звонил тому, кто звонка не просил.
        Обнулить счётчик может только исходящее, а в режиме подсказки бот
        исходящих не шлёт — то есть само это не рассасывалось.

        Разделитель заведён давно и надёжен: запись Авито — `sender_type='avito'`
        (`inbound._AVITO_SYSTEM_SENDER`), наша собственная — `'system'`
        (`handoff.add_system_message`).
        """
        if self.db is None or self.conv is None:
            return [], 0
        rows = list(
            (
                await self.db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == self.conv.id,
                        or_(
                            and_(
                                Message.direction == "note",
                                Message.body.like(SUGGEST_PREFIX + "%"),
                            ),
                            Message.direction == "system",
                            # ⚠ ИСХОДЯЩИЕ ТОЖЕ ЧИТАЕМ, И ТОЛЬКО РАДИ СЧЁТА ЗВОНКОВ:
                            # они обнуляют счётчик, см. ниже. В подсказки не идут.
                            Message.direction == "out",
                        ),
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(max(1, limit) * 3)
                )
            ).scalars()
        )
        подсказки: list[str] = []
        звонков = 0
        for msg in reversed(rows):
            тело = (msg.body or "").strip()
            if msg.direction == "out":
                # ⚠ ЗВОНОК, ПОСЛЕ КОТОРОГО МЫ ОТВЕТИЛИ, — ЗАКРЫТЫЙ ЗВОНОК (26.08).
                # Считались ВСЕ звонки за окно, и один давний набор глушил бота в этом
                # диалоге навсегда. Снимок владельца: клиент звонил в 01:02, оператор
                # ответил в 03:05, клиент в 03:34 спросил «завтра вечером сможете
                # приехать посмотреть?» — а бот молча ушёл к человеку с ярлыком «клиент
                # просит позвонить». Разговор давно продолжился текстом; звонок закрыт
                # ровно тем, что мы после него написали.
                звонков = 0
                continue
            if not тело:
                continue
            if msg.direction == "note":
                подсказки.append(тело[len(SUGGEST_PREFIX) :].strip())
            elif msg.sender_type == _AVITO_SENDER and _CALL_MARK in тело.lower():
                звонков += 1
        return [x for x in подсказки if x], звонков

    async def client_texts(self, limit: int = CLASSIFY_CONTEXT_MESSAGES) -> list[str]:
        """Последние входящие клиента (маскированные) — для классификатора."""
        rows = list(
            (
                await self.db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == self.conv.id,
                        Message.direction == "in",
                        Message.sender_type == "client",
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(max(1, limit))
                )
            ).scalars()
        )
        texts = [mask_phones(m.body) for m in reversed(rows) if (m.body or "").strip()]
        if not texts and self.last_incoming:
            texts = [mask_phones(self.last_incoming)]
        return texts

    async def _latest_incoming_id(self) -> Any:
        """Последнее входящее диалога — маркер свежести ответа (30.08)."""
        return (
            await self.db.execute(
                select(Message.id)
                .where(Message.conversation_id == self.conv.id, Message.direction == "in")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _latest_out_id(self) -> Any:
        """Последняя реплика клиенту — бота или оператора."""
        return (
            await self.db.execute(
                select(Message.id)
                .where(Message.conversation_id == self.conv.id, Message.direction == "out")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _ai_request(self, step_id: str, context_messages: int) -> AiRequest | None:
        """Контекст вызова модели: переписка, имя, город, канал. `None` — модели нет.

        Отдельно от самого вызова (проверка 24.09): тик собирает контекст под
        блокировкой строки диалога, а к модели идёт уже без неё.
        """
        backend = self.ai
        if backend is None:
            return None
        # лид-боту — сырой текст (его воронка ищет номер), своему Claude — маска
        dialog = await self.load_dialog_for_ai(
            context_messages, mask=provider_of(self.bot) != LEADBOT
        )
        # Запоминаем ровно то, что ушло на ту сторону: журнал лид-бота
        # показывает вопрос клиента, и он обязан совпадать с вопросом,
        # который бот на самом деле видел.
        #
        # ⚠ ТЕЛЕФОНЫ ЗДЕСЬ НЕ ЗАМАСКИРОВАНЫ, если провайдер — лид-бот: строкой
        # выше ему выбран `mask=False`. Раньше этот комментарий утверждал
        # обратное и стоял ровно в том месте, где читатель решает, безопасно
        # ли трогать журнал. Маскировку для журнала делает `_record_leadbot`
        # отдельным вызовом `mask_phones` при записи — не полагайтесь на то,
        # что содержимое `_last_ai_dialog` уже чистое.
        self._last_ai_dialog = dialog
        # ⚠ 15.08: имя и город перестали уходить пустыми. Город решает филиал,
        # колонку прайса и окно приезда; имя — живое обращение. Дефолт «Клиент»
        # именем не считается: это заглушка карточки, а не то, как человека зовут.
        client_name, city = "", ""
        try:
            # песочница живёт на фейковой беседе без БД — контекст добываем
            # только если он есть, его отсутствие не стоит сорванного вызова
            _cid = getattr(self.conv, "client_id", None)
            if _cid is not None and self.db is not None:
                cl = await self.db.get(Client, _cid)
                client_name = (cl.name or "").strip() if cl else ""
                if client_name.lower() == "клиент":
                    client_name = ""
        except Exception:  # noqa: BLE001
            client_name = ""
        try:
            city = город_диалога(self.conv)
        except Exception:  # noqa: BLE001
            city = ""
        # ⚠ 16.08: канальные данные для заявки. Источник («В95»), номер
        # партнёра и ссылка отзыва живут в карточке канала — лид-бот кладёт
        # их в заявку (комментарий партнёра и 🟢-хвост), сам он их не знает.
        channel: dict[str, str] | None = None
        try:
            _aid = getattr(self.conv, "account_id", None)
            if _aid is not None and self.db is not None:
                acc = await self.db.get(AvitoAccount, _aid)
                if acc is not None:
                    channel = {
                        "origin": (acc.lead_origin or "").strip(),
                        "partner": (acc.lead_partner_number or "").strip(),
                        "review_url": (acc.review_url or "").strip(),
                        # 17.08, несвязываемость аккаунтов: у каждого канала —
                        # своя манера мастера, стабильно от id аккаунта.
                        # ⚠ У лид-бота их ДЕВЯТЬ (p1..p9, brain/prompt.py):
                        # модуль 7 отдавал девяти каналам семь голосов —
                        # два совпадения гарантированно (аудит 18.08).
                        "persona": f"p{int(acc.id.int % 9) + 1}",
                    }
        except Exception:  # noqa: BLE001 — заявка без канала лучше, чем без ответа
            channel = None
        # ⚠ 24.08: то, чего нет в переписке — свои прошлые подсказки и звонки
        # клиента. Собираем ТОЛЬКО для лид-бота: у нашего Claude контракт другой,
        # и лишние поля ему передавать нечем.
        # ⚠ ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ — ТОЛЬКО НА ПУТЬ ЛИД-БОТА, И ТОЛЬКО ОТДЕЛЬНЫМ
        # СЛОВАРЁМ. Безусловные kwargs роняли шесть тестов: у нашего Claude
        # (`app/bots/ai.py`) и у песочницы такого контракта нет, и `TypeError`
        # съедался общим `except` — то есть вызов молча превращался в
        # «AI недоступен» и handoff. Провайдер у бота выбирается настройкой,
        # значит расширять контракт можно только у того, кому он принадлежит.
        _доп: dict[str, Any] = {}
        if provider_of(self.bot) == LEADBOT:
            try:
                подсказки, звонков = await self.load_bot_context(context_messages)
                _доп = {"prior_suggestions": подсказки, "calls": звонков}
            except Exception:  # noqa: BLE001 — ответ без контекста лучше, чем без ответа
                _доп = {}
        return AiRequest(
            step_id=step_id,
            backend=backend,
            bot=self.bot,
            dialog=dialog,
            item_title=self.conv.item_title,
            kwargs={
                "client_name": client_name,
                "city": city,
                "channel": channel,
                "conv_key": str(getattr(self.conv, "id", "") or ""),
                **_доп,
            },
            last_in_id=await self._latest_incoming_id(),
            last_out_id=await self._latest_out_id(),
            last_incoming=self.last_incoming,
            series=list(self.series),
            pending_classification=self.pending_classification,
            suggest_once=bool(getattr(self, "_suggest_once", False)),
        )

    async def _call_ai_answer(self, context_messages: int) -> dict[str, Any] | None:
        """Вызов модели на месте — путь песочницы и тестов движка (`defer_ai=False`)."""
        try:
            request = await self._ai_request(self.state.step or "", context_messages)
        except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
            log.warning("bot.ai_answer_failed", error=type(exc).__name__)
            return None
        return await request.ask() if request is not None else None

    async def extract_entities(self) -> dict[str, Any] | None:
        """Эшелон 2 извлечения перед handoff (02 §3.5), best effort.

        Телефон берём ТОЛЬКО из локальной регулярки: в модель текст уходит уже
        маскированным, поэтому её `phone` игнорируем осознанно.
        """
        backend = self.ai
        extract = getattr(backend, "extract_entities", None) or getattr(backend, "extract", None)
        if extract is None:
            return None
        try:
            texts = await self.client_texts(limit=20)
            if not texts:
                return None
            result = await extract(texts)
        except Exception as exc:  # noqa: BLE001 — best effort, тик не роняем
            log.warning("bot.extract_failed", error=type(exc).__name__)
            return None
        if not isinstance(result, dict):
            return None
        entities = {k: v for k, v in result.items() if k != "phone" and v not in (None, "")}
        for key, value in entities.items():  # не перетираем собранное `ask`-ами
            self.state.vars.setdefault(key, value)
        return entities

    async def classify_and_react(self, text: str | None = None) -> dict[str, Any] | None:
        """Условие №3 (негатив) и второй эшелон условия №1 (02 §3.4) — одним вызовом.

        Путь песочницы. Тик бота зовёт части порознь: тексты и реакцию — в
        транзакции, классификатор — между ними, без блокировки строки диалога
        (`runtime._classify_after_commit`, проверка 24.09).
        """
        if text:
            self.last_incoming = text
        backend = self.ai
        if backend is None or not hasattr(backend, "classify_message"):
            return None
        try:
            texts = await self.client_texts()
        except Exception as exc:  # noqa: BLE001
            log.warning("bot.classify_failed", error=type(exc).__name__)
            return None
        result = await classify_texts(backend, texts)
        if result is None:
            return None
        await self.react_to_classification(result)
        return result

    async def react_to_classification(self, result: dict[str, Any]) -> None:
        """Реакция на оценку классификатора: тег негатива, передача человеку."""
        if result.get("sentiment") == "negative":
            added = add_conversation_tags(self.conv, [NEGATIVE_TAG])
            if added:
                self.conv.updated_at = self.now()
                self.outbox.event(
                    "conversation:updated",
                    conversation_updated_event(
                        self.conv,
                        {"tags": list(self.conv.tags or []), "bot_active": self.conv.bot_active},
                    ),
                )
            # Если бот уже не активен — только тег: handoff не повторяем.
            if self.conv.bot_active and not self.state.handoff_done():
                await self.do_handoff("negative")
            elif added:
                # Диалог уже у человека по другой причине — передавать нечего, а
                # сказать про недовольство надо: сама передача про него не знала.
                await handoff_mod.notify_negative(self.db, self.conv, self.outbox)
        elif result.get("wants_human"):
            if self.conv.bot_active and not self.state.handoff_done():
                await self.do_handoff("client_request")

    # -------------------------------------------------------------- телефон

    async def capture_phone(self, text: str | None) -> str | None:
        """Эшелон 1 (02 §3.5): регулярка на каждом входящем, нулевая цена.

        Номер живёт локально: `bot_vars.vars` + карточка клиента. В промпт он
        не попадает (см. :func:`app.bots.steps.mask_phones`).
        """
        phone = extract_phone(text)
        if not phone:
            return None
        self.state.vars.setdefault("phone", phone)
        client = await self.db.get(Client, self.conv.client_id)
        if client is not None and not client.phone:
            client.phone = phone
            await write_audit(
                self.db,
                user_id=None,
                action="client.phone_captured",
                entity="client",
                entity_id=str(client.id),
                details={"conversation_id": str(self.conv.id), "source": "bot"},
            )
        return phone


async def classify_texts(backend: Any, texts: list[str]) -> dict[str, Any] | None:
    """Сам поход в классификатор. Отказ — `None`: контур вспомогательный."""
    if backend is None or not hasattr(backend, "classify_message") or not texts:
        return None
    try:
        result = await backend.classify_message(texts)
    except Exception as exc:  # noqa: BLE001
        log.warning("bot.classify_failed", error=type(exc).__name__)
        return None
    return result if isinstance(result, dict) else None


# --------------------------------------------------------------- мелочи


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
