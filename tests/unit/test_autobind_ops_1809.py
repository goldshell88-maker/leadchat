"""Обвязка автопривязки адреса (18.09, участок D): настройки, воронка, монитор,
догон, аудит.

Что стережётся:
- реестр настроек знает ВСЕ ключи контракта и их умолчания; мусор в таблице
  даёт умолчание, а `NULL` под текстом вопроса — пустую строку, которую
  отправитель подменяет умолчанием Spec (`address_ask.settings_from`);
- ручка настроек пишет новые ключи, оставляет след в журнале и не пускает
  текст вопроса без слова об адресе;
- воронка считает на стенде из живых строк (SQLite + `REGEXP` из фикстуры)
  и партиция карточки сходится; правка руками поверх автоадреса считается по
  журналу без человека и только над автозаписью ТОЙ ЖЕ недели (одна рамка с
  `card_auto` в тревоге `edits`); сравнение недель по порогам; текст тревоги
  без прошлой недели — «первая неделя замера»;
- задача планировщика КОММИТИТ явно: без этого `session_scope` откатывает и
  ровная неделя терялась бы — проверка НОВОЙ сессией после закрытия старой;
- строка воронки в мониторе: нет снимка / ровно / падение;
- команды: `address-funnel --store` коммитит, `address-audit-sample` печатает
  маски (телефон — `address_llm.mask`, имя клиента — `[имя]`) и фильтрует по
  степени ДО потолка, `address-autofill-backlog` ставит по степеням и лестнице
  (направление — `clients.grade_beats`), `address-recheck` сбрасывает строку карты.

Диверсии (обязаны краснеть): убрать `await db.commit()` из задачи — тест
(а) не находит строку недели; убрать `user_id IS NULL` из признака автозаписи
— правка поверх подтверждённого кнопкой посчитается; убрать вторую ветку
догона — «карточку держит худшая степень» не ставится.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import typer

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message, Notification
from app.models.address_funnel import AddressFunnelWeek
from app.scheduler.jobs import address_funnel as funnel_jobs
from app.services import address_funnel, api_monitor, app_settings, geocode
from app.services import notifications as notify_svc
from app.services.audit import msk_day_range

pytestmark = pytest.mark.anyio


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- стенд -------------------------------------------------------------------------


@pytest.fixture
async def regexp(engine) -> None:  # noqa: ANN001
    """`REGEXP` для SQLite — на ЭТОМ соединении: StaticPool держит одно, и
    функция переживает сессии; регистрировать на `connect` поздно —
    соединение уже открыто `create_all`."""
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.create_function(
            "regexp", 2, lambda p, s: s is not None and re.search(p, s) is not None
        )


НЕДЕЛЯ = date(2026, 9, 7)  # понедельник
SINCE, UNTIL = address_funnel.week_bounds(НЕДЕЛЯ)
В_ОКНЕ = SINCE + timedelta(days=2, hours=10)


async def _диалог(
    s: Any,
    account_id: uuid.UUID,
    *,
    текст: str | None,
    когда: datetime = В_ОКНЕ,
    строка: dict[str, Any] | None = None,
    карточка: dict[str, Any] | None = None,
) -> tuple[Client, Conversation, ClientAddressCandidate | None]:
    client = Client(channel="avito", external_id=uuid.uuid4().hex[:10], name="Клиент")
    s.add(client)
    await s.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=uuid.uuid4().hex[:12],
        account_id=account_id,
        client_id=client.id,
        status="new",
        last_message_at=когда,
    )
    s.add(conv)
    await s.flush()
    if текст is not None:
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=uuid.uuid4().hex[:12],
                direction="in",
                sender_type="client",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=когда,
            )
        )
    row: ClientAddressCandidate | None = None
    if строка is not None:
        row = ClientAddressCandidate(
            client_id=client.id,
            conversation_id=conv.id,
            value=строка.get("value", "ул Ленина, 5"),
            street=строка.get("street", "ул Ленина"),
            house=строка.get("house", "5"),
            raw=строка.get("raw", текст or "ул Ленина 5"),
            level="A",
            status=строка.get("status", "accepted"),
            detected_at=строка.get("detected_at", когда),
            resolved_at=строка.get("resolved_at"),
            resolved_by_id=строка.get("resolved_by_id"),
            kind=строка.get("kind", "house"),
            geo_status=строка.get("geo_status", "exact"),
            geo_provider=строка.get("geo_provider", "dadata"),
            geo_formatted=строка.get("geo_formatted", "улица Ленина, 5, Город"),
            geo_lat=строка.get("geo_lat", 55.0),
            geo_lon=строка.get("geo_lon", 37.0),
        )
        s.add(row)
        await s.flush()
    if карточка is not None:
        client.address = карточка.get("address", "улица Ленина, 5, Город")
        client.address_set_at = карточка.get("address_set_at")
        client.address_candidate_id = (
            row.id if карточка.get("source", True) and row is not None else None
        )
    await s.flush()
    return client, conv, row


async def _журнал(
    s: Any,
    *,
    action: str,
    entity_id: str | None,
    user_id: uuid.UUID | None,
    details: dict[str, Any],
    когда: datetime,
) -> None:
    s.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity="client",
            entity_id=entity_id,
            details=details,
            created_at=когда,
        )
    )


@pytest.fixture
async def стенд(db_sessionmaker, make_avito_account, regexp):  # noqa: ANN001
    """Девять диалогов проекта §6 + журнал: dialogs=7, with_row=6, card=6."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        # (1) адресная реплика + строка + автокарточка, точная точка.
        c1, *_ = await _диалог(
            s, account.id, текст="ул Ленина 5, подъезд 2", строка={}, карточка={}
        )
        # (2) + строка с приблизительной точкой.
        await _диалог(
            s,
            account.id,
            текст="проспект Мира 10",
            строка={"geo_provider": "dadata~approx"},
            карточка={},
        )
        # (3) + строка без точки: отказ карты со строкой улицы (STREET_KNOWN).
        await _диалог(
            s,
            account.id,
            текст="улица Победы 3",
            строка={
                "geo_status": "house_missing",
                "geo_lat": None,
                "geo_lon": None,
                "geo_formatted": "улица Победы, 3, Город",
            },
            карточка={"address": "улица Победы, 3, Город"},
        )
        # (4) адресная реплика без строки.
        await _диалог(s, account.id, текст="переулок Садовый 7")
        # (5) реплика без адреса — не считается.
        await _диалог(s, account.id, текст="Здравствуйте, сколько стоит замена экрана?")
        # (6) карточка руками.
        await _диалог(
            s,
            account.id,
            текст="ул Гагарина 12",
            строка={},
            карточка={"address_set_at": В_ОКНЕ},
        )
        # (7) служебная подпись Авито — не считается.
        await _диалог(s, account.id, текст="Вот подробности: ул Пушкина 1, цена 500")
        # (8) строка есть, а карточка без источника (ушёл каскадом).
        await _диалог(
            s,
            account.id,
            текст="шоссе Энтузиастов 20",
            строка={},
            карточка={"source": False},
        )
        # (9) место: kind=place, exact, с хвостом — приблизительно по контракту.
        await _диалог(
            s,
            account.id,
            текст="снт Солнечный 15",
            строка={"kind": "place", "geo_provider": "dadata~approx"},
            карточка={},
        )
        # Правка руками поверх автоадреса в окне — считается…
        await _журнал(
            s,
            action="client.address_captured",
            entity_id=str(c1.id),
            user_id=None,
            details={"source": "geocoder", "address": "улица Ленина, 5, Город"},
            когда=В_ОКНЕ,
        )
        await _журнал(
            s,
            action="client.address_edited",
            entity_id=str(c1.id),
            user_id=uuid.uuid4(),
            details={
                "source": "manual",
                "previous": "улица Ленина, 5, Город",
                "address": "улица Ленина, 5, кв 3",
            },
            когда=В_ОКНЕ + timedelta(hours=1),
        )
        # …такая же за окном — нет…
        await _журнал(
            s,
            action="client.address_edited",
            entity_id=str(c1.id),
            user_id=uuid.uuid4(),
            details={
                "source": "manual",
                "previous": "улица Ленина, 5, Город",
                "address": "улица Ленина, 5, кв 4",
            },
            когда=UNTIL + timedelta(hours=1),
        )
        # …правка в окне поверх автоадреса ПРОШЛОГО месяца — нет: тревога
        # `edits` делит на автокарточки недели, рамка у числителя та же
        # (ревью 19.09; ДИВЕРСИЯ: снять `a.created_at >= since` — станет 2)…
        давний = uuid.uuid4()
        await _журнал(
            s,
            action="client.address_captured",
            entity_id=давний.hex,
            user_id=None,
            details={"source": "geocoder", "address": "улица Старая, 9, Город"},
            когда=SINCE - timedelta(days=20),
        )
        await _журнал(
            s,
            action="client.address_edited",
            entity_id=давний.hex,
            user_id=uuid.uuid4(),
            details={"source": "manual", "previous": "улица Старая, 9, Город", "address": "y"},
            когда=В_ОКНЕ + timedelta(hours=3),
        )
        # …и поверх подтверждённого КНОПКОЙ (запись с человеком) — тоже нет.
        чужой = uuid.uuid4()
        await _журнал(
            s,
            action="client.address_captured",
            entity_id=чужой.hex,
            user_id=uuid.uuid4(),
            details={"source": "regex", "address": "улица Мира, 1, Город"},
            когда=В_ОКНЕ,
        )
        await _журнал(
            s,
            action="client.address_edited",
            entity_id=чужой.hex,
            user_id=uuid.uuid4(),
            details={"source": "manual", "previous": "улица Мира, 1, Город", "address": "x"},
            когда=В_ОКНЕ + timedelta(hours=2),
        )
        # Система спросила адрес — один раз в окне, один раз за окном.
        for когда in (В_ОКНЕ, UNTIL + timedelta(days=1)):
            s.add(
                AuditLog(
                    user_id=None,
                    action=address_funnel.ASKED_ACTION,
                    entity="conversation",
                    entity_id=uuid.uuid4().hex,
                    details={},
                    created_at=когда,
                )
            )
        await s.commit()
    return account


# --- настройки ---------------------------------------------------------------------


async def test_умолчания_контракта(db) -> None:  # noqa: ANN001
    assert await app_settings.get(db, app_settings.ADDRESS_GEO_AUTO_DECIDE) is True
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_ENABLED) is False
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_DELAY_SEC) == 600
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_MIN_CHARS) == 25
    текст = await app_settings.get(db, app_settings.ADDRESS_ASK_TEXT)
    assert текст == app_settings.ADDRESS_ASK_DEFAULT_TEXT
    # Имена ключей — ровно из контракта п.7: их читают участки A и C.
    assert app_settings.ADDRESS_GEO_AUTO_DECIDE == "address_geo.auto_decide"
    assert {
        app_settings.ADDRESS_ASK_ENABLED,
        app_settings.ADDRESS_ASK_DELAY_SEC,
        app_settings.ADDRESS_ASK_TEXT,
        app_settings.ADDRESS_ASK_MIN_CHARS,
    } == {
        "address_ask.enabled",
        "address_ask.delay_sec",
        "address_ask.text",
        "address_ask.min_chars",
    }


def test_умолчательный_текст_вопроса_читается_как_вопрос_об_адресе() -> None:
    """Иначе ответ клиента на наш же вопрос не поднялся бы адресом."""
    from app.services import inbound

    assert inbound._ВОПРОС_ОБ_АДРЕСЕ.search(app_settings.ADDRESS_ASK_DEFAULT_TEXT)
    assert "адрес" in app_settings.ADDRESS_ASK_DEFAULT_TEXT
    for слово in ("город", "посёлок", "улиц", "дом", "подъезд"):
        assert слово in app_settings.ADDRESS_ASK_DEFAULT_TEXT


async def test_мусор_в_таблице_даёт_умолчание_а_null_под_текстом_пустую_строку(
    db, db_sessionmaker
) -> None:  # noqa: ANN001
    from app.models import AppSetting

    async with db_sessionmaker() as s:
        s.add(AppSetting(key=app_settings.ADDRESS_GEO_AUTO_DECIDE, value="да"))
        s.add(AppSetting(key=app_settings.ADDRESS_ASK_DELAY_SEC, value="600"))
        s.add(AppSetting(key=app_settings.ADDRESS_ASK_TEXT, value=None))
        await s.commit()
    assert await app_settings.get(db, app_settings.ADDRESS_GEO_AUTO_DECIDE) is True
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_DELAY_SEC) == 600
    # Край контракта с участком C: `NULL` в таблице — это "" у реестра, а
    # отправитель пустой текст подменяет умолчанием Spec (`settings_from`),
    # не выходит и не молчит — как написано у `ADDRESS_ASK_TEXT`.
    from app.services import address_ask

    assert await app_settings.get(db, app_settings.ADDRESS_ASK_TEXT) == ""
    всё = await app_settings.get_all(db)
    assert всё[app_settings.ADDRESS_ASK_TEXT] == ""
    assert address_ask.settings_from(всё).text == app_settings.ADDRESS_ASK_DEFAULT_TEXT


async def test_ручка_пишет_ключи_и_оставляет_след(client, tokens, db_sessionmaker) -> None:  # noqa: ANN001
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"auto_decide": False, "ask_delay_sec": 300, "ask_min_chars": 0},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["auto_decide"] is False
    assert (res.json()["ask_delay_sec"], res.json()["ask_min_chars"]) == (300, 0)
    again = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert again.json()["auto_decide"] is False and again.json()["ask_delay_sec"] == 300
    async with db_sessionmaker() as s:
        (строка,) = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .all()
        )
    assert строка.details["before"]["auto_decide"] is True
    assert строка.details["after"]["auto_decide"] is False
    assert строка.details["after"]["ask_delay_sec"] == 300


async def test_текст_вопроса_без_слова_об_адресе_отвергается(client, tokens, db) -> None:  # noqa: ANN001
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"ask_text": "Здравствуйте, во сколько удобно?"},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["details"]["fields"][0]["rule"] == "address_word"
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_TEXT) == (
        app_settings.ADDRESS_ASK_DEFAULT_TEXT
    )
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"ask_text": "  Куда подъехать мастеру?  "},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["ask_text"] == "Куда подъехать мастеру?"


async def test_границы_задержки_и_порога_держит_ручка(client, tokens) -> None:  # noqa: ANN001
    for тело in ({"ask_delay_sec": 60}, {"ask_delay_sec": 4000}, {"ask_min_chars": -1}):
        res = await client.patch(
            "/api/v1/settings/address-detect", json=тело, headers=hdr(tokens["admin"])
        )
        assert res.status_code in (400, 422), (тело, res.text)


# --- воронка: замер --------------------------------------------------------------


async def test_замер_на_стенде_и_партиция_карточки(db, стенд) -> None:  # noqa: ANN001
    c = await address_funnel.measure(db, since=SINCE, until=UNTIL)
    assert (c.dialogs, c.with_row, c.card) == (7, 6, 6)
    assert (c.card_auto_exact, c.card_auto_approx, c.card_auto_text) == (1, 2, 1)
    assert (c.card_person, c.card_unknown) == (1, 1)
    assert (
        c.card_auto_exact + c.card_auto_approx + c.card_auto_text + c.card_person + c.card_unknown
        == c.card
    )
    assert c.edited_after_auto == 1
    assert c.asked == 1
    # Соседняя неделя пуста — окно режет по created_at.
    пусто = await address_funnel.measure(db, since=UNTIL, until=UNTIL + address_funnel.WEEK)
    assert пусто.dialogs == 0 and пусто.asked == 1


async def test_степень_в_воронке_судит_card_grade(db, стенд, monkeypatch) -> None:  # noqa: ANN001
    """Диверсия «один судья»: подменили судью — счётчики поехали вслед."""
    monkeypatch.setattr(geocode, "card_grade", lambda *a, **k: geocode.GRADE_TEXT)
    c = await address_funnel.measure(db, since=SINCE, until=UNTIL)
    assert (c.card_auto_exact, c.card_auto_approx, c.card_auto_text) == (0, 0, 4)


def test_невод_адресоподобной_реплики() -> None:
    assert address_funnel.address_like("ул Ленина 5")
    assert address_funnel.address_like("Проспект Мира, д. 10")
    assert address_funnel.address_like("снт Солнечный 15")
    assert not address_funnel.address_like("улыбка 5")  # «ул» внутри слова
    assert not address_funnel.address_like("ул Ленина")  # без цифры
    assert not address_funnel.address_like("Вот подробности: ул Пушкина 1")
    assert not address_funnel.address_like(None)


# --- воронка: сравнение, недели, хранение ------------------------------------------


def _c(**kw: int) -> address_funnel.FunnelCounts:
    return address_funnel.FunnelCounts(**kw)


def test_сравнение_недель_по_порогам() -> None:
    прошлая = _c(dialogs=120, with_row=100, card=72)
    assert address_funnel.compare(прошлая, _c(dialogs=120, with_row=100, card=54)) == ["card_share"]
    assert address_funnel.compare(прошлая, _c(dialogs=120, with_row=100, card=66)) == []
    # Мало диалогов — шум, тревоги нет.
    assert address_funnel.compare(_c(dialogs=40, card=24), _c(dialogs=40, card=10)) == []
    assert address_funnel.compare(прошлая, _c(dialogs=120, with_row=70, card=72)) == ["row_share"]
    # Правок 6 из 50 автокарточек.
    assert address_funnel.compare(
        None, _c(dialogs=10, card_auto_exact=30, card_auto_approx=20, edited_after_auto=6)
    ) == ["edits"]
    assert address_funnel.compare(None, _c(dialogs=200, card=1)) == []
    assert "п.п." in address_funnel.reason_words(["card_share"])


def test_границы_недели_по_москве() -> None:
    since, until = address_funnel.week_bounds(НЕДЕЛЯ)
    ожидание = msk_day_range(НЕДЕЛЯ, НЕДЕЛЯ + timedelta(days=6))
    assert (since, until) == ожидание
    with pytest.raises(ValueError):
        address_funnel.week_bounds(date(2026, 9, 8))
    # Понедельник 00:30 МСК — «эта» неделя только началась, полная — прошлая.
    now = datetime(2026, 9, 13, 21, 30, tzinfo=UTC)  # 14.09 00:30 МСК
    assert address_funnel.last_complete_week(now) == НЕДЕЛЯ
    assert address_funnel.parse_week("", now) == НЕДЕЛЯ
    assert address_funnel.parse_week("2026-09-07") == НЕДЕЛЯ
    with pytest.raises(ValueError):
        address_funnel.parse_week("2026-09-09")


def test_снимок_из_словаря_терпит_старую_форму() -> None:
    c = address_funnel.FunnelCounts.from_dict({"dialogs": 3, "card": 2, "лишнее": 9})
    assert (c.dialogs, c.card, c.card_unknown) == (3, 2, 0)
    assert address_funnel.FunnelCounts.from_dict(c.as_dict()) == c


async def test_хранение_upsert_и_правило_прошлой_недели(db) -> None:  # noqa: ANN001
    now = datetime.now(UTC)
    await address_funnel.store(db, week_start=НЕДЕЛЯ, counts=_c(dialogs=1), computed_at=now)
    await address_funnel.store(db, week_start=НЕДЕЛЯ, counts=_c(dialogs=2), computed_at=now)
    await db.commit()
    assert (
        await db.execute(sa.select(sa.func.count()).select_from(AddressFunnelWeek))
    ).scalar_one() == 1
    assert (await address_funnel.stored(db, НЕДЕЛЯ)) == _c(dialogs=2)
    assert await address_funnel.stored(db, НЕДЕЛЯ - address_funnel.WEEK) is None
    # Пропущенная неделя: соседняя строка — позапрошлая, prev обязан быть None.
    await address_funnel.store(
        db, week_start=НЕДЕЛЯ + 2 * address_funnel.WEEK, counts=_c(dialogs=3), computed_at=now
    )
    await db.commit()
    свежая = await address_funnel.latest(db)
    assert свежая is not None and свежая.counts == _c(dialogs=3) and свежая.prev is None
    недели = await address_funnel.recent(db, weeks=2)
    assert [n.week_start for n in недели] == [НЕДЕЛЯ + 2 * address_funnel.WEEK, НЕДЕЛЯ]
    assert all(n.prev is None for n in недели)
    # Соседняя неделя есть — prev находится.
    await address_funnel.store(
        db, week_start=НЕДЕЛЯ + address_funnel.WEEK, counts=_c(dialogs=5), computed_at=now
    )
    await db.commit()
    свежая = await address_funnel.latest(db)
    assert свежая is not None and свежая.prev == _c(dialogs=5)
    with pytest.raises(ValueError):
        await address_funnel.store(db, week_start=date(2026, 9, 9), counts=_c(), computed_at=now)


# --- задача планировщика --------------------------------------------------------------


@pytest.fixture
def оснастка_задачи(monkeypatch, db_sessionmaker, redis):  # noqa: ANN001
    monkeypatch.setattr(funnel_jobs.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(funnel_jobs.db_mod, "session_scope", db_sessionmaker)


def test_задача_регистрируется_по_понедельникам() -> None:
    from apscheduler.triggers.cron import CronTrigger

    вызовы: list[dict] = []

    class Планировщик:
        def add_job(self, func, trigger, **kw):  # noqa: ANN001
            вызовы.append({"func": func, "trigger": trigger, **kw})

    funnel_jobs.register(Планировщик())
    (вызов,) = вызовы
    assert вызов["func"] is funnel_jobs.measure_last_week
    assert вызов["id"] == funnel_jobs.JOB_ID == "address_funnel_weekly"
    assert isinstance(вызов["trigger"], CronTrigger)
    assert str(вызов["trigger"].fields[4]) == "mon"  # day_of_week
    assert str(вызов["trigger"].fields[5]) == "1"  # hour, UTC → 04:40 МСК
    assert вызов["max_instances"] == 1 and вызов["misfire_grace_time"] >= 3600


def test_планировщик_зовёт_register_воронки() -> None:
    import ast
    import inspect
    import textwrap

    from app.scheduler import main as scheduler_main

    дерево = ast.parse(textwrap.dedent(inspect.getsource(scheduler_main)))
    зовут = {
        f"{getattr(у.func.value, 'id', '')}.{у.func.attr}"
        for у in ast.walk(дерево)
        if isinstance(у, ast.Call) and isinstance(у.func, ast.Attribute)
    }
    assert "address_funnel_jobs.register" in зовут


def test_вид_уведомления_заведён() -> None:
    spec = notify_svc.KINDS[funnel_jobs.KIND]
    assert (spec.severity, spec.audience) == ("warning", "admin")


def test_текст_уведомления_без_прошлой_недели_говорит_первая_неделя() -> None:
    """Тревога по правкам возможна и без прошлой недели: «против 0 %» читалось
    бы как падение с нуля. С прошлой неделей — сравнение долей."""
    текущая = _c(dialogs=120, card=60, card_auto_exact=60, edited_after_auto=7)
    тело = funnel_jobs.notification_body(НЕДЕЛЯ, текущая, None)
    assert "первая неделя замера" in тело and "против" not in тело
    assert "50 %" in тело and "правок руками поверх автоадреса той же недели 7" in тело
    тело = funnel_jobs.notification_body(НЕДЕЛЯ, текущая, _c(dialogs=100, card=70))
    assert "против 70 % неделей раньше" in тело and "первая неделя" not in тело


async def test_ровная_неделя_остаётся_в_базе_благодаря_явному_commit(
    db_sessionmaker, оснастка_задачи, regexp, monkeypatch
) -> None:  # noqa: ANN001
    """(а) Тревоги нет → `notify` не зовётся; строка недели обязана остаться.
    Диверсия: убрать `await db.commit()` в задаче — тест краснеет."""
    неделя = address_funnel.last_complete_week()

    async def не_зовётся(*a: Any, **k: Any) -> Any:
        raise AssertionError("на ровной неделе уведомления нет")

    monkeypatch.setattr(funnel_jobs.notify_svc, "notify", не_зовётся)
    counts = await funnel_jobs.measure_last_week()
    assert counts.dialogs == 0
    async with db_sessionmaker() as s:  # НОВАЯ сессия после закрытия старой
        assert await address_funnel.stored(s, неделя) == counts


async def test_падение_доли_шлёт_уведомление_и_не_глотает_сбой_центра(
    db_sessionmaker, оснастка_задачи, regexp, make_avito_account, monkeypatch
) -> None:  # noqa: ANN001
    неделя = address_funnel.last_complete_week()
    since, _ = address_funnel.week_bounds(неделя)
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        # Прошлая неделя — 120 диалогов, 72 в карточке; эта — 100 диалогов без карточек.
        await address_funnel.store(
            s,
            week_start=неделя - address_funnel.WEEK,
            counts=_c(dialogs=120, with_row=100, card=72),
            computed_at=datetime.now(UTC),
        )
        for i in range(100):
            await _диалог(
                s, account.id, текст=f"ул Ленина {i + 1}", когда=since + timedelta(hours=i)
            )
        await s.commit()
    counts = await funnel_jobs.measure_last_week()
    assert counts.dialogs == 100 and counts.card == 0
    async with db_sessionmaker() as s:
        assert await address_funnel.stored(s, неделя) == counts
        (row,) = (await s.execute(sa.select(Notification))).scalars().all()
    assert (row.kind, row.severity) == (funnel_jobs.KIND, "warning")
    assert row.title == notify_svc.KINDS[funnel_jobs.KIND].title
    assert "100" in (row.body or "") and "60 %" in (row.body or "")

    # (б) Центр уведомлений упал — исключение наружу, а строка недели уже есть.
    async def падает(*a: Any, **k: Any) -> Any:
        raise RuntimeError("центр лежит")

    monkeypatch.setattr(funnel_jobs.notify_svc, "notify", падает)
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(AddressFunnelWeek).where(AddressFunnelWeek.week_start == неделя))
        await s.commit()
    with pytest.raises(RuntimeError):
        await funnel_jobs.measure_last_week()
    async with db_sessionmaker() as s:
        assert await address_funnel.stored(s, неделя) is not None


async def test_ошибка_замера_не_оставляет_строки_и_не_глотается(
    db_sessionmaker, оснастка_задачи, monkeypatch
) -> None:  # noqa: ANN001
    async def падает(*a: Any, **k: Any) -> Any:
        raise RuntimeError("таймаут")

    monkeypatch.setattr(funnel_jobs.address_funnel, "measure", падает)
    with pytest.raises(RuntimeError):
        await funnel_jobs.measure_last_week()
    async with db_sessionmaker() as s:
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(AddressFunnelWeek))
        ).scalar_one() == 0


# --- монитор -------------------------------------------------------------------------


def _по_ключу(items: list[dict], key: str) -> dict:
    return next(r for r in items if r["key"] == key)


async def test_строка_воронки_в_мониторе(db, redis) -> None:  # noqa: ANN001
    items = await api_monitor.overview(db, redis)
    строка = items[-1]
    assert строка["key"] == api_monitor.FUNNEL_KEY == "address_funnel"
    assert (строка["state"], строка["url"], строка["editable"]) == (
        api_monitor.STATE_UNKNOWN,
        None,
        False,
    )
    assert "понедельник" in строка["state_note"]
    assert api_monitor.FUNNEL_KEY in api_monitor.BUILTIN_KEYS
    # Пробы у строки нет — «Проверить» её пропускает, ручка отдаст 404.
    assert await api_monitor.probe_spec(db, redis, api_monitor.FUNNEL_KEY) is None

    now = datetime.now(UTC)
    await address_funnel.store(
        db,
        week_start=НЕДЕЛЯ - address_funnel.WEEK,
        counts=_c(dialogs=200, with_row=180, card=150),
        computed_at=now,
    )
    await address_funnel.store(
        db,
        week_start=НЕДЕЛЯ,
        counts=_c(
            dialogs=200,
            with_row=180,
            card=140,
            card_auto_exact=100,
            card_auto_approx=30,
            card_auto_text=10,
        ),
        computed_at=now,
    )
    await db.commit()
    строка = _по_ключу(await api_monitor.overview(db, redis), api_monitor.FUNNEL_KEY)
    assert строка["state"] == api_monitor.STATE_OK
    assert "70 %" in строка["state_note"] and "75 %" in строка["state_note"]
    assert "точных 100" in строка["notes"] and "приблизительных 30" in строка["notes"]

    await address_funnel.store(
        db, week_start=НЕДЕЛЯ, counts=_c(dialogs=200, with_row=180, card=100), computed_at=now
    )
    await db.commit()
    строка = _по_ключу(await api_monitor.overview(db, redis), api_monitor.FUNNEL_KEY)
    assert строка["state"] == api_monitor.STATE_DOWN
    assert строка["state_note"].startswith("падение: ")


async def test_монитор_через_api_отдаёт_строку_воронки_и_не_даёт_её_проверить(
    client, tokens
) -> None:  # noqa: ANN001
    res = await client.get("/api/v1/settings/apis", headers=hdr(tokens["admin"]))
    assert res.status_code == 200, res.text
    assert res.json()["items"][-1]["key"] == api_monitor.FUNNEL_KEY
    assert list(res.json()) == ["items"]
    res = await client.post(
        f"/api/v1/settings/apis/{api_monitor.FUNNEL_KEY}/check", headers=hdr(tokens["admin"])
    )
    assert res.status_code == 404


async def test_недоступная_таблица_снимков_не_роняет_монитор(db, redis, monkeypatch) -> None:  # noqa: ANN001
    async def падает(*a: Any, **k: Any) -> Any:
        raise sa.exc.OperationalError("select", {}, Exception("no such table"))

    monkeypatch.setattr(api_monitor.address_funnel, "latest", падает)
    строка = _по_ключу(await api_monitor.overview(db, redis), api_monitor.FUNNEL_KEY)
    assert строка["state"] == api_monitor.STATE_DOWN
    assert "OperationalError" in строка["state_note"]


# --- команды -------------------------------------------------------------------------


def test_команды_видны_в_списке_и_обёрнуты_в_run() -> None:
    from app.cli import app as cli_app

    имена = {c.name for c in cli_app.registered_commands}
    assert {
        "address-funnel",
        "address-audit-sample",
        "address-autofill-backlog",
        "address-recheck",
    } <= имена


async def test_address_funnel_store_коммитит_а_без_store_не_пишет(
    db_sessionmaker, стенд, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_funnel

    async with db_sessionmaker() as s:
        await run_address_funnel(s, week=НЕДЕЛЯ.isoformat(), store=False)
    async with db_sessionmaker() as s:
        assert await address_funnel.stored(s, НЕДЕЛЯ) is None
    async with db_sessionmaker() as s:
        await run_address_funnel(s, week=НЕДЕЛЯ.isoformat(), store=True, history=3)
    вывод = capsys.readouterr().out
    assert "dialogs\t7" in вывод and "снимок недели записан" in вывод
    assert "тревоги нет" in вывод and НЕДЕЛЯ.isoformat() in вывод
    async with db_sessionmaker() as s:  # новая сессия после закрытия старой
        снимок = await address_funnel.stored(s, НЕДЕЛЯ)
    assert снимок is not None and (снимок.dialogs, снимок.card) == (7, 6)
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_funnel(s, week="2026-09-09", store=False)
    assert exc.value.exit_code == 2


def _таблица(out: str) -> list[str]:
    """Строки TSV команды без строк журнала (structlog пишет JSON в stdout)."""
    return [х for х in out.splitlines() if "\t" in х and not х.startswith("{")]


def test_маска_адреса_прячет_личное_и_оставляет_улицу_и_дом() -> None:
    """Телефон — через `address_llm.mask` (один разборщик номеров
    `phone_parse.find_all`, не вторая регулярка): «⁷⁹00 123 45 67» надстрочными и
    «8 900 123 45 67» без плюса вторая регулярка не ловила бы одинаково.
    Имя клиента из карточки → `[имя]`; короткое слово имени не вырезается."""
    from app.cli import _маска_адреса
    from app.services import address_llm

    маска = _маска_адреса("ул Ленина 5, кв 12, домофон 1234, подъезд 3, +7 900 123-45-67")
    assert "ул Ленина 5" in маска
    assert "кв **" in маска and "домофон **" in маска and "подъезд **" in маска
    assert "XXXX" in маска and "12" not in маска and "1234" not in маска and "900" not in маска
    assert _маска_адреса(None) == ""
    # Тот же маскировщик, что у модели: надстрочные цифры и номер без плюса.
    for текст in ("звоните ⁷⁹⁰⁰1234567", "8 900 123 45 67 Ленина 5"):
        assert "900" not in _маска_адреса(текст) and "X" * 5 in _маска_адреса(текст)
        assert _маска_адреса(текст) == " ".join(address_llm.mask(текст).split())
    # Имя клиента: слова ≥ 3 букв, без учёта регистра, целым словом.
    маска = _маска_адреса("Это Пётр Адресов, ул адресова 5, петров", имя="Пётр Адресов")
    assert "Пётр" not in маска and "Адресов" not in маска and маска.count("[имя]") == 2
    assert "ул адресова 5" in маска and "петров" in маска
    assert _маска_адреса("Ли пишет: ул Ли 5", имя="Ли") == "Ли пишет: ул Ли 5"


async def test_address_audit_sample_печатает_автокарточки_масками(
    db_sessionmaker, make_avito_account, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_audit_sample

    account = await make_avito_account()
    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        общее = {"resolved_at": now - timedelta(days=1)}
        await _диалог(
            s,
            account.id,
            текст="ул Ленина 5 кв 12, +7 900 111-22-33",
            строка={**общее},
            карточка={},
        )
        await _диалог(
            s,
            account.id,
            текст="пр Мира 10",
            строка={**общее, "geo_provider": "dadata~approx"},
            карточка={},
        )
        await _диалог(
            s,
            account.id,
            текст="ул Победы 3",
            строка={**общее, "geo_status": "not_found", "geo_lat": None, "geo_lon": None},
            карточка={},
        )
        await _диалог(
            s, account.id, текст="снт Солнечный", строка={**общее, "kind": "place"}, карточка={}
        )
        # Руками и кнопкой — не попадают; старая автозапись — за окном.
        await _диалог(
            s, account.id, текст="ул Гагарина 1", строка={**общее}, карточка={"address_set_at": now}
        )
        await _диалог(
            s,
            account.id,
            текст="ул Кирова 2",
            строка={**общее, "resolved_by_id": uuid.uuid4()},
            карточка={},
        )
        await _диалог(
            s,
            account.id,
            текст="ул Старая 9",
            строка={"resolved_at": now - timedelta(days=30)},
            карточка={},
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_audit_sample(s, days=7, limit=50, degree="all")
    вывод = _таблица(capsys.readouterr().out)
    assert вывод[0].startswith("№\t")
    строки = вывод[1:]
    assert len(строки) == 4
    assert sorted(х.split("\t")[2] for х in строки) == ["approx", "approx", "exact", "text"]
    первая = next(х for х in строки if "\texact\t" in х)
    # Номер стережём по колонке текста клиента, а не по всей строке: в хвосте
    # стоит время `detected_at`, и «111» встречается в микросекундах (выкатка
    # 20.09 упала на «…:02.821119» — ложная тревога сторожа маски).
    текст_клиента = первая.split("\t")[5]
    assert "кв **" in текст_клиента and "XXXX" in текст_клиента and "111" not in текст_клиента
    async with db_sessionmaker() as s:
        await run_address_audit_sample(s, days=7, limit=50, degree="approx")
    assert len(_таблица(capsys.readouterr().out)) == 3
    async with db_sessionmaker() as s:
        await run_address_audit_sample(s, days=7, limit=900, degree="text")
    assert len(_таблица(capsys.readouterr().out)) == 2
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit):
            await run_address_audit_sample(s, days=7, limit=50, degree="foo")
    async with db_sessionmaker() as s:
        await run_address_audit_sample(s, days=0, limit=50, degree="all")
    assert "нет" in capsys.readouterr().out


async def test_address_audit_sample_фильтр_степени_до_потолка(
    db_sessionmaker, make_avito_account, capsys
) -> None:  # noqa: ANN001
    """`--degree approx --limit 3` при трёх approx и четырёх exact даёт ровно
    три строки. ДИВЕРСИЯ: вернуть `.limit(limit)` в SQL — потолок режет ДО
    степени, и из трёх случайных строк approx окажется 0–3 (все три — 1/35)."""
    from app.cli import run_address_audit_sample

    account = await make_avito_account()
    общее = {"resolved_at": datetime.now(UTC) - timedelta(days=1)}
    async with db_sessionmaker() as s:
        for i in range(3):
            await _диалог(
                s,
                account.id,
                текст=f"пр Мира {i + 1}",
                строка={**общее, "geo_provider": "dadata~approx"},
                карточка={},
            )
        for i in range(4):
            await _диалог(s, account.id, текст=f"ул Ленина {i + 1}", строка={**общее}, карточка={})
        await s.commit()
    for _ in range(3):
        async with db_sessionmaker() as s:
            await run_address_audit_sample(s, days=7, limit=3, degree="approx")
        строки = _таблица(capsys.readouterr().out)[1:]
        assert len(строки) == 3
        assert {х.split("\t")[2] for х in строки} == {"approx"}
    async with db_sessionmaker() as s:
        await run_address_audit_sample(s, days=7, limit=2, degree="all")
    assert len(_таблица(capsys.readouterr().out)[1:]) == 2


async def test_address_autofill_backlog_ставит_по_степеням_и_лестнице(
    db_sessionmaker, make_avito_account, redis, monkeypatch, capsys
) -> None:  # noqa: ANN001
    from app import cli

    account = await make_avito_account()
    ждущая = {"status": "pending", "resolved_at": None}
    async with db_sessionmaker() as s:
        # Пустая карточка: дом exact, место exact, отказ со строкой улицы — ставятся.
        _, c_дом, _ = await _диалог(s, account.id, текст="ул Ленина 5", строка={**ждущая})
        _, c_место, _ = await _диалог(
            s, account.id, текст="снт Солнечный", строка={**ждущая, "kind": "place"}
        )
        _, c_текст, _ = await _диалог(
            s,
            account.id,
            текст="ул Победы 3",
            строка={**ждущая, "geo_status": "house_missing", "geo_lat": None, "geo_lon": None},
        )
        # Отказ БЕЗ строки улицы — степени нет, не ставится.
        _, c_пусто, _ = await _диалог(
            s,
            account.id,
            текст="ул Тихая 1",
            строка={
                **ждущая,
                "geo_status": "not_found",
                "geo_lat": None,
                "geo_lon": None,
                "geo_formatted": None,
            },
        )
        # Карточку держит приблизительная строка автоматики, ждёт точный дом — ставится.
        client, c_лучше, _ = await _диалог(
            s, account.id, текст="пр Мира 10", строка={"geo_provider": "dadata~approx"}, карточка={}
        )
        s.add(
            ClientAddressCandidate(
                client_id=client.id,
                conversation_id=c_лучше.id,
                value="пр Мира, 10",
                street="пр Мира",
                house="10",
                raw="пр Мира 10",
                level="A",
                status="pending",
                detected_at=В_ОКНЕ,
                kind="house",
                geo_status="exact",
                geo_provider="dadata",
                geo_formatted="проспект Мира, 10, Город",
                geo_lat=55.0,
                geo_lon=37.0,
            )
        )
        # Карточку держит точный дом, ждёт строка без точки — НЕ ставится (лестница).
        client2, c_хуже, _ = await _диалог(
            s, account.id, текст="ул Гагарина 12", строка={}, карточка={}
        )
        s.add(
            ClientAddressCandidate(
                client_id=client2.id,
                conversation_id=c_хуже.id,
                value="ул Гагарина, 12",
                street="ул Гагарина",
                house="12",
                raw="ул Гагарина 12",
                level="A",
                status="pending",
                detected_at=В_ОКНЕ,
                kind="house",
                geo_status="house_missing",
                geo_provider="dadata",
                geo_formatted="улица Гагарина, 12, Город",
                geo_lat=None,
                geo_lon=None,
            )
        )
        # Руками — не ставится, даже с точным домом в очереди.
        _, c_руками, _ = await _диалог(
            s,
            account.id,
            текст="ул Кирова 2",
            строка={**ждущая},
            карточка={"address_set_at": В_ОКНЕ, "source": False},
        )
        # Подтверждено кнопкой — не ставится.
        client4, c_кнопкой, _ = await _диалог(
            s,
            account.id,
            текст="ул Садовая 4",
            строка={"resolved_by_id": uuid.uuid4(), "geo_provider": "dadata~approx"},
            карточка={},
        )
        s.add(
            ClientAddressCandidate(
                client_id=client4.id,
                conversation_id=c_кнопкой.id,
                value="ул Садовая, 4",
                street="ул Садовая",
                house="4",
                raw="ул Садовая 4",
                level="A",
                status="pending",
                detected_at=В_ОКНЕ,
                kind="house",
                geo_status="exact",
                geo_provider="dadata",
                geo_formatted="улица Садовая, 4, Город",
                geo_lat=55.0,
                geo_lon=37.0,
            )
        )
        await s.commit()

    поставлено: list[uuid.UUID] = []

    async def enqueue(r: Any, cid: uuid.UUID) -> None:
        поставлено.append(cid)

    monkeypatch.setattr("app.services.geocode_queue.enqueue_autofill", enqueue)
    monkeypatch.setattr(cli.redis_mod, "get_client", lambda: redis)
    async with db_sessionmaker() as s:
        await cli.run_address_autofill_backlog(s, dry_run=True)
    assert поставлено == []
    assert "всего к постановке: 4" in capsys.readouterr().out
    async with db_sessionmaker() as s:
        await cli.run_address_autofill_backlog(s, dry_run=False)
    assert set(поставлено) == {c_дом.id, c_место.id, c_текст.id, c_лучше.id}
    assert not {c_пусто.id, c_хуже.id, c_руками.id, c_кнопкой.id} & set(поставлено)
    assert "поставлено (без подтверждения от очереди): 4" in capsys.readouterr().out

    # Очередь молчит о сбое (enqueue_autofill возвращает None) — команда не падает.
    async def молчит(r: Any, cid: uuid.UUID) -> None:
        return None

    monkeypatch.setattr("app.services.geocode_queue.enqueue_autofill", молчит)
    async with db_sessionmaker() as s:
        await cli.run_address_autofill_backlog(s, dry_run=False)
    assert "поставлено (без подтверждения от очереди): 4" in capsys.readouterr().out


async def test_address_recheck_сбрасывает_строку_карты(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    from app.cli import run_address_recheck

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        _, _, row = await _диалог(
            s,
            account.id,
            текст="ул Победы 3",
            строка={
                "status": "pending",
                "geo_status": "house_missing",
                "geo_lat": None,
                "geo_lon": None,
                "geo_formatted": "улица Победы, 3, Город",
            },
        )
        await s.commit()
        assert row is not None
        row_id = row.id
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="house_missing", dry_run=False)
    async with db_sessionmaker() as s:
        снова = await s.get(ClientAddressCandidate, row_id)
    assert снова is not None
    assert (снова.geo_status, снова.geo_formatted, снова.geo_attempts) == ("pending", None, 0)
    assert (
        geocode.card_grade(
            снова.kind,
            снова.geo_status,
            снова.geo_provider,
            снова.geo_lat,
            снова.geo_lon,
            снова.geo_formatted,
        )
        is None
    )
