"""Лид-бот — отдельный раздел, а не один из ботов (решение владельца 12.08).

Здесь сторожатся ровно те свойства, ради которых раздел и заводился, и каждое
из них ломается молча: список ботов однажды покажет системную запись, редактор
однажды её откроет, токен однажды уедет в браузер, а канал чужого бота однажды
переприпишут без предупреждения. Ни одно из этих событий не падает и не
подсвечивается — их видно только специально.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.leadbot import LeadbotReply
from app.models import AvitoAccount, Bot
from app.models.leadbot import (
    OUTCOME_BRIDGE,
    OUTCOME_HINT,
    OUTCOME_LOW_CONFIDENCE,
    OUTCOME_SENT,
    OUTCOME_UNAVAILABLE,
    LeadbotCall,
)
from app.services import leadbot_admin, leadbot_log


def admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


# --------------------------------------------------- граница с разделом ботов


async def test_system_bot_is_absent_from_the_bots_list(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Списка ботов лид-бот не касается — ни строкой, ни счётчиком.

    Счётчик проверяется отдельно от строк: «ботов: 1» при пустом списке читается
    как потерянная строка, и искать её пошли бы в пагинации.
    """
    async with db_sessionmaker() as session:
        await leadbot_admin.ensure_system_bot(session)
        await session.commit()

    response = await client.get("/api/v1/bots", headers=admin(tokens))
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["page"]["total"] == 0


async def test_system_bot_does_not_open_in_the_editor(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Редактор сценариев лид-бота не открывает — 404, а не 403.

    403 означал бы «есть, но не для вас» и подсказывал бы существование записи,
    которой в интерфейсе ботов нет. А главное: у лид-бота сценарий из трёх
    шагов, и правка его руками сломала бы разговор, который ведёт чужая сторона.
    """
    async with db_sessionmaker() as session:
        bot = await leadbot_admin.ensure_system_bot(session)
        await session.commit()
        bot_id = bot.id

    # ВСЕ ручки с `{bot_id}`, а не три на выбор. Дыра здесь — это не «можно
    # посмотреть»: через `enable` системную запись включили бы в обход раздела,
    # через `accounts` переприписали бы ей каналы, через `PUT` переписали бы
    # сценарий, который ведёт чужая сторона. Список сверен с картой маршрутов.
    for method, path in (
        ("GET", f"/api/v1/bots/{bot_id}"),
        ("PUT", f"/api/v1/bots/{bot_id}"),
        ("DELETE", f"/api/v1/bots/{bot_id}"),
        ("POST", f"/api/v1/bots/{bot_id}/enable"),
        ("POST", f"/api/v1/bots/{bot_id}/disable"),
        ("PUT", f"/api/v1/bots/{bot_id}/accounts"),
    ):
        # Тело правдоподобное: пустое отсекается проверкой схемы ДО
        # обработчика, и охрану такой запрос не доказывает вовсе.
        body: dict[str, Any] = (
            {"account_ids": []}
            if path.endswith("/accounts")
            else {"name": "Попытка", "scenario": {"version": 1, "entry": "s", "steps": []}}
        )
        response = await client.request(method, path, headers=admin(tokens), json=body)
        assert response.status_code == 404, f"{method} {path} → {response.status_code}"


# ------------------------------------------------------------------- секреты


async def test_token_never_leaves_the_server(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
) -> None:
    """Токен не возвращается НИ ОДНОЙ ручкой раздела — только «задан или нет».

    Он открывает чужому сервису всю переписку клиентов. Уехав в браузер, он
    ложится в историю запросов, в кэш вкладки и в чужие снимки экрана — и
    заметить это по поведению системы невозможно.
    """
    # Латиница: заголовки HTTP — latin-1, и токен с кириллицей `save`
    # отклоняет осознанно (см. его комментарий).
    secret = "s3cr3t-leadbot-token-12345"
    saved = await client.put(
        "/api/v1/leadbot/connection",
        headers=admin(tokens),
        json={"url": "http://10.10.0.2:8790", "token": secret},
    )
    assert saved.status_code == 200
    assert secret not in saved.text

    overview = await client.get("/api/v1/leadbot", headers=admin(tokens))
    assert overview.status_code == 200
    assert secret not in overview.text
    # Но факт наличия виден: без него человек не отличит «не настроено» от
    # «настроено, но не работает», а это разные починки.
    assert overview.json()["connection"]["token_set"] is True


async def test_empty_token_means_keep_the_old_one(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
) -> None:
    """Сохранение формы без токена не стирает токен.

    Экран не знает нынешнего значения — показать его нельзя. Значит пустое поле
    означает «не трогали»; трактуй его как «убрать», и любое исправление
    опечатки в адресе молча разорвало бы связь.
    """
    await client.put(
        "/api/v1/leadbot/connection",
        headers=admin(tokens),
        json={"url": "http://10.10.0.2:8790", "token": "first-token"},
    )
    await client.put(
        "/api/v1/leadbot/connection",
        headers=admin(tokens),
        json={"url": "http://10.10.0.2:9999"},
    )
    overview = (await client.get("/api/v1/leadbot", headers=admin(tokens))).json()
    assert overview["connection"]["token_set"] is True
    assert overview["connection"]["url"] == "http://10.10.0.2:9999"


# --------------------------------------------------------------------- каналы


async def test_channel_of_another_bot_is_not_taken_silently(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Занятый канал не переприписывается — 409 с человеческой причиной.

    Молчаливая переприписка означала бы, что список каналов лид-бота работает
    как способ выключить чужую автоматику: тот бот перестал бы отвечать, и
    заметили бы это по жалобам клиентов, а не по экрану.
    """
    account = await make_avito_account(title="Занятый")
    async with db_sessionmaker() as session:
        other = Bot(
            id=uuid.uuid4(),
            name="Обычный бот",
            scenario={"version": 1, "entry": "s", "steps": []},
        )
        session.add(other)
        await session.flush()
        bound = await session.get(AvitoAccount, account.id)
        assert bound is not None
        bound.bot_id = other.id
        await session.commit()

    response = await client.patch(
        "/api/v1/leadbot",
        headers=admin(tokens),
        json={"account_ids": [str(account.id)]},
    )
    assert response.status_code == 409
    assert "другой бот" in response.text

    # И канал остался у прежнего хозяина — отказ обязан быть полным.
    async with db_sessionmaker() as session:
        again = await session.get(AvitoAccount, account.id)
        assert again is not None
        assert again.bot_id is not None
        system = await leadbot_admin.system_bot(session)
        assert system is None or again.bot_id != system.id


async def test_new_leadbot_is_off_and_only_suggests(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
) -> None:
    """Первое открытие раздела не включает ничего.

    Иначе сохранение адреса — действие, которое человек считает подготовкой, —
    пустило бы чужой регламент к живым клиентам девяти аккаунтов.
    """
    overview = (await client.get("/api/v1/leadbot", headers=admin(tokens))).json()
    assert overview["enabled"] is False
    assert overview["mode"] == "suggest"


# --------------------------------------------------------------------- журнал


def _reply(**meta: Any) -> LeadbotReply:
    return LeadbotReply(
        request_id="rq-1",
        ms=120,
        status=200,
        answer={"reply": "Ремонтируем, от 1500 ₽", "confidence": 0.9, "needs_operator": False},
        raw={"meta": meta},
    )


@pytest.mark.parametrize(
    "outcome",
    [OUTCOME_SENT, OUTCOME_HINT, OUTCOME_BRIDGE, OUTCOME_LOW_CONFIDENCE, OUTCOME_UNAVAILABLE],
)
async def test_every_outcome_is_storable(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    outcome: str,
) -> None:
    """Каждый исход проходит CHECK базы.

    Список исходов выписан дважды — в модели и текстом в миграции (иначе
    содержимое применённой миграции зависело бы от версии кода). Разъезд этих
    двух списков не падает при записи ровно того исхода, который забыли; ловится
    он только перебором.
    """
    async with db_sessionmaker() as session:
        await leadbot_log.record(
            session,
            _reply(layer="роутер"),
            outcome=outcome,
            conversation_id=None,
            account_id=None,
            question="Ремонтируете холодильники?",
        )
        await session.commit()
        rows = (await session.execute(select(LeadbotCall))).scalars().all()
        assert [r.outcome for r in rows] == [outcome]


async def test_journal_keeps_the_reason_not_just_the_fact(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Эскалация ложится с причиной, подписью и сроком.

    Ради этого журнал и заводился: «бот передал человеку» без причины и срока —
    это то же, что было в логах, только в базе.
    """
    async with db_sessionmaker() as session:
        await leadbot_log.record(
            session,
            _reply(
                layer="модель",
                escalation={"reason": "visit_failed", "label": "срыв визита", "deadline_min": 5},
                lead_ready=True,
                warnings=["воронка не увидит номер"],
            ),
            outcome=OUTCOME_BRIDGE,
            conversation_id=None,
            account_id=None,
            question="Мастер не приехал",
        )
        await session.commit()
        row = (await session.execute(select(LeadbotCall))).scalar_one()

    assert row.layer == "модель"
    assert row.escalation_reason == "visit_failed"
    assert row.escalation_label == "срыв визита"
    assert row.escalation_deadline_min == 5
    assert row.lead_ready is True
    assert row.warnings == ["воронка не увидит номер"]
    assert row.ms == 120


async def test_journal_stores_a_trimmed_copy_not_the_correspondence(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Вопрос и ответ обрезаются.

    Журнал отвечает на «почему бот ответил так», а полная переписка уже лежит в
    `messages`. Вторая её копия — это второе место, откуда переписка клиентов
    способна утечь, и второе, которое надо чистить по сроку.
    """
    long_question = "а" * 5000
    async with db_sessionmaker() as session:
        await leadbot_log.record(
            session,
            _reply(),
            outcome=OUTCOME_SENT,
            conversation_id=None,
            account_id=None,
            question=long_question,
        )
        await session.commit()
        row = (await session.execute(select(LeadbotCall))).scalar_one()

    assert row.question is not None
    assert len(row.question) < 500
    assert row.question.endswith("…")


async def test_journal_failure_never_breaks_the_tick(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Сломанная запись журнала не бросает наружу ничего.

    Решение владельца №4: недоступность ИИ никогда не блокирует доставку.
    Журнал — побочная польза; уронив тик, он отнял бы у клиента ответ ради
    нашей бухгалтерии. Ссылка на несуществующий диалог — самый дешёвый способ
    сломать вставку по-настоящему, а не подменой.
    """
    async with db_sessionmaker() as session:
        await leadbot_log.record(
            session,
            _reply(),
            outcome=OUTCOME_SENT,
            conversation_id=uuid.uuid4(),  # такого диалога нет — нарушение FK
            account_id=None,
            question="вопрос",
            now=datetime.now(UTC),
        )
    # Дошли сюда — значит наружу ничего не вылетело. Это и проверяется.


# ------------------------------------------------------- тестовый разговор


async def test_test_chat_sends_nothing_to_anyone(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Тестовый разговор не создаёт ни сообщений, ни записей журнала.

    Ради этого свойства он и заводится: посмотреть, что бот отвечает, ДО того
    как пустить его к живым людям. Попади проверка в журнал работы — он
    перестал бы отвечать на вопрос «как бот вёл себя в бою».
    """
    response = await client.post(
        "/api/v1/leadbot/test",
        headers=admin(tokens),
        json={"dialog": [{"role": "user", "content": "Ремонтируете холодильники?"}]},
    )
    assert response.status_code == 200
    # Связь не настроена — честный отказ, а не выдуманный ответ.
    assert response.json()["ok"] is False

    async with db_sessionmaker() as session:
        assert (await session.execute(select(LeadbotCall))).scalars().all() == []


# ----------------------------------------------------------------------- права


LEADBOT_ENDPOINTS = (
    ("GET", "/api/v1/leadbot", None),
    ("PATCH", "/api/v1/leadbot", {}),
    ("PUT", "/api/v1/leadbot/connection", {"url": "http://10.10.0.2:8790"}),
    ("POST", "/api/v1/leadbot/connection/reset", {}),
    ("POST", "/api/v1/leadbot/probe", {}),
    ("POST", "/api/v1/leadbot/test", {"dialog": []}),
    ("GET", "/api/v1/leadbot/calls", None),
    ("GET", "/api/v1/leadbot/silence", None),
)


@pytest.mark.parametrize("role", ["head", "manager", "observer"])
async def test_only_admin_touches_the_leadbot(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    role: str,
) -> None:
    """Весь раздел — только администратору, включая чтение журнала.

    ПОЧЕМУ ДАЖЕ РУКОВОДИТЕЛЮ НЕЛЬЗЯ СМОТРЕТЬ. Соблазн открыть журнал шире
    понятен: это же просто чтение. Но в нём лежат реплики клиентов и решения по
    ним, а право `bots:manage` в проекте означает «отвечает за то, что система
    говорит клиентам от имени сервиса». Разведи чтение и настройку — и появится
    роль, которая видит переписку, ни за что не отвечая.

    На эту проверку ссылается комментарий в `tests/unit/test_rbac.py`: там эти
    ручки вынесены из общей матрицы, потому что изменяющим нужно тело, а `probe`
    и `test` ходят по сети в чужой сервис.
    """
    headers = {"Authorization": f"Bearer {tokens[role]}"}
    for method, path, body in LEADBOT_ENDPOINTS:
        response = await client.request(method, path, headers=headers, json=body)
        assert response.status_code == 403, f"{method} {path} для {role} → {response.status_code}"


async def test_silence_explains_why_the_bot_took_nothing(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker,
    make_avito_account,
) -> None:
    """«Включил бота — он ничего не взял»: система обязана назвать причину.

    ЖАЛОБА ВЛАДЕЛЬЦА 19.08. Бот вёл себя правильно: он входит только в СВЕЖИЙ
    диалог и только туда, где человек ещё не отвечал. За те часы новых диалогов
    не было — обращения приходили в существующие, которые уже вёл человек.
    Но узнать это было неоткуда: журнал обращений пуст, а пустота читается как
    поломка. Теперь причина считается тем же кодом, что решает пускать бота.
    """
    from app.models import Client, Conversation

    account = await make_avito_account()
    async with db_sessionmaker() as db:
        client_row = Client(id=uuid.uuid4(), channel="avito", external_id="900100")
        db.add(client_row)
        await db.flush()
        db.add(
            Conversation(
                id=uuid.uuid4(),
                channel="avito",
                external_chat_id="chat-silence",
                account_id=account.id,
                client_id=client_row.id,
                status="closed",  # диалог уже ведёт человек — боту сюда нельзя
                bot_active=False,
                bot_vars={},
                tags=[],
                unread_count=0,
                declined_by=[],
                last_message_at=datetime.now(UTC),
            )
        )
        await db.commit()

    ответ = await client.get(
        "/api/v1/leadbot/silence", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()

    assert тело["in_progress"] == [], "бот сейчас ничего не ведёт"
    (строка,) = [x for x in тело["not_taken"] if x["status"] == "closed"]
    assert строка["reason"] in ("not_new", "no_bot"), строка
    assert строка["reason_label"], "причина обязана быть переведена на человеческий"
    assert "диалог" in строка["reason_label"] or "канал" in строка["reason_label"]
