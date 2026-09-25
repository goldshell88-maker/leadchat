"""Пакет 7а, Q24 (21.09): эхо адреса мастерской — без строк, свои адреса
выводятся сами (программа §2.2, контракт `contract_7a.md` §C).

* `address_own` — ключ «улица+дом» (`street_core` + `house_key`), список
  своих адресов из двух настроек (владелец + задача), упоминание по тексту;
* `inbound._maybe_extract_address`: под правилом разбора `workshop_echo` дом,
  который клиент повторил за нашей исходящей того же диалога (раньше первого
  клиентского упоминания) или из списка своих, строкой не заводится (К-5):
  `on` — журнал `address.workshop_echo` без слов клиента; `shadow` — строка со
  следом `stop_shadow`; `off` — как сегодня и ни одного лишнего запроса;
* задача `address_own_addresses_weekly`: ключ из исходящих ≥ 3 диалогов или
  ≥ 2 аккаунтов за 90 дней, в каждом раньше клиента → настройка
  `address_geo.own_addresses_auto` + журнал, только при изменении;
* ручка `/settings/address-detect`: `own_addresses` проверяется по частям
  (400 с текстом части), `own_addresses_auto` — только показ.

ДИВЕРСИИ (каждая обязана краснеть; прогнаны 21.09 «правка → тест → откат»):
снять условие `!= PARSE_OFF` у ветки эха в `_maybe_extract_address` —
`test_off_эхо_пишется_как_сегодня_и_исходящие_не_читаются` (шпион `execute`);
`_эхо_мастерской` без проверки «клиент называл раньше» (сразу `ЭХО_ДИАЛОГА`) —
`test_клиент_назвал_раньше_исходящей_строка_есть`; ветка эха игнорирует
`under_rule` (пишет строку и при `on`) — `test_on_исходящая_раньше_клиента_
строки_нет_и_журнал`; задача считает диалог без проверки порядка
(`_клиент_называл` → False) — `test_задача_собирает_реестр_и_пишет_настройку`
(`conversations == 3`, а не 4); задача пишет настройку и при неизменном
реестре — тот же тест (второй прогон без строки журнала); курсор порции по
одному `created_at` вместо пары `(created_at, id)` —
`test_порции_по_паре_created_at_id_и_потолок`; `Spec` без `check` у
`address_own_addresses` — `test_ручка_own_addresses_мусор_400_с_текстом_части`;
убрать `address_own_jobs.register(scheduler)` из `build_scheduler` —
`test_задача_зарегистрирована_в_планировщике`.

Тексты вымышленные, телефонов и имён нет.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message
from app.scheduler.jobs import address_own_addresses as job
from app.services import address_own, address_parse, app_settings
from app.services import inbound as inbound_svc
from app.services.inbound import apply_inbound_event
from tests.unit import test_autobind_ops_1809 as обвязка
from tests.unit import test_paket2_history_1909 as история

pytestmark = pytest.mark.anyio

# Фикстуры соседних файлов — присваиванием, не импортом имени (ruff F811).
без_сети = история.без_сети
regexp = обвязка.regexp
T0 = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)
ПРАВИЛО = address_parse.WORKSHOP_ECHO
ЛЕНИНА_5 = (("ленина",), "5")


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(история.ACCOUNT_UID)


async def _настройка(db_sessionmaker: Any, **пары: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, dict(пары), user_id=None)
        await s.commit()


async def _исходящее(
    db_sessionmaker: Any, текст: str, when: datetime, *, chat: str = "chat-n29"
) -> None:
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == chat))
        ).scalar_one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"out-{when.timestamp()}",
                direction="out",
                sender_type="operator",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def _строки(db_sessionmaker: Any) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
                )
            ).scalars()
        )


def _шпион_execute(monkeypatch: pytest.MonkeyPatch, db: Any) -> list[str]:
    """Тексты всех запросов сессии: сито исходящих узнаётся по `<regexp>` —
    больше в пути входящего `regexp_match` никто не зовёт."""
    записи: list[str] = []
    исходное = db.execute

    async def шпион(stmt: Any, *a: Any, **kw: Any) -> Any:
        записи.append(str(stmt))
        return await исходное(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", шпион)
    return записи


def _эхо_в_журнале(логи: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [л for л in логи if л["event"] == "address.workshop_echo"]


# ── address_own: ключ, список, упоминание ─────────────────────────────────────


def test_ключ_один_на_все_написания() -> None:
    """«ул. Ленина 5», «Ленина, 5», «ленина 5» — один ключ; тип улицы в ключ не
    входит; литера и корпус входят (`house_key`)."""
    ключи = {
        address_own.key_of(address_parse.parse(т, про_адрес=True))
        for т in ("ул. Ленина 5", "Ленина, 5", "ленина 5", "проспект Ленина, д. 5")
    }
    assert ключи == {ЛЕНИНА_5}
    assert address_own.key_of(address_parse.parse("Ленина 5 к 2")) == (("ленина",), "5к2")
    assert address_own.key_of(address_parse.parse("Ленина 5к2")) == (("ленина",), "5к2")
    assert address_own.key_of(address_parse.parse("Ленина 7а")) != address_own.key_of(
        address_parse.parse("Ленина 7")
    )
    # Не дом — не ключ: место без улицы, пусто, `None`.
    assert address_own.key_of(address_parse.parse("массив Южный")) is None
    assert address_own.key_of(address_parse.parse("Телевизор Самсунг 55")) is None
    assert address_own.key_of(None) is None


def test_список_режется_по_запятой_но_дом_приклеивается_к_улице() -> None:
    """«ул. Ленина, 5» — естественная запись; запятая перед домом — не
    разделитель. Так же читается вывод задачи «улица, дом; улица, дом»."""
    assert address_own.split_list("ул. Ленина, 5; Мира 10, пр. Победы, 7а\nНевская 7а") == [
        "ул. Ленина 5",
        "Мира 10",
        "пр. Победы 7а",
        "Невская 7а",
    ]
    assert address_own.split_list("ул. Ленина, 5 к 2; Садовая, 17/2") == [
        "ул. Ленина 5 к 2",
        "Садовая 17/2",
    ]
    assert address_own.split_list(None) == [] and address_own.split_list(" ; , ") == []
    ключи = address_own.own_keys("ул. Ленина, 5; мусор", "ул. Мира, 10; Невская 7а")
    assert ключи == {ЛЕНИНА_5, (("мира",), "10"), (("невская",), "7а")}
    assert address_own.is_own(address_parse.parse("Ленина 5, подъеду"), ключи)
    assert not address_own.is_own(address_parse.parse("Ленина 15, подъеду"), ключи)
    assert not address_own.is_own(None, ключи)


def test_упоминание_по_тексту_а_не_по_строкам() -> None:
    """Реплики, которые разбор не берёт, для порядка «кто первый» считаются."""
    assert address_parse.parse("да это дом 5 по ленина", про_адрес=True) is None
    assert address_own.text_mentions("да это дом 5 по ленина", ЛЕНИНА_5)
    assert address_own.text_mentions("по ленина в 5 доме", ЛЕНИНА_5)
    assert address_own.text_mentions("ЛЕНИНА д.5", ЛЕНИНА_5)
    assert address_own.text_mentions("ленина 5 к 2", (("ленина",), "5к2"))
    assert address_own.text_mentions("ленина 5/2", (("ленина",), "5к2"))
    # Другой дом, другая улица, число внутри числа, пусто — нет.
    assert not address_own.text_mentions("ленина 15", ЛЕНИНА_5)
    assert not address_own.text_mentions("ленина 5к2", ЛЕНИНА_5)
    assert not address_own.text_mentions("мира 5", ЛЕНИНА_5)
    assert not address_own.text_mentions("", ЛЕНИНА_5)
    assert not address_own.text_mentions(None, ЛЕНИНА_5)


def test_проверка_списка_называет_негодную_часть_и_не_ходит_в_базу() -> None:
    check = app_settings.SPECS[app_settings.ADDRESS_OWN_ADDRESSES].check
    assert check is not None and not inspect.iscoroutinefunction(check)
    check("ул Ленина 5; Мира, 10\nпр. Победы 7а")
    check("")
    with pytest.raises(ValueError, match="«мусор»"):
        check("ул Ленина 5; мусор")
    with pytest.raises(ValueError, match="«ул Ленина»"):
        check("ул Ленина")  # улица без дома — тоже не адрес списка
    # Вывод задачи человек не правит — реестр его содержимое не проверяет.
    assert app_settings.SPECS[app_settings.ADDRESS_OWN_ADDRESSES_AUTO].check is None
    assert address_parse.PARSE_RULES[ПРАВИЛО] == address_parse.PARSE_OFF


# ── сторож эха во входящем ────────────────────────────────────────────────────


async def test_off_эхо_пишется_как_сегодня_и_исходящие_не_читаются(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: Any,
    без_сети: Any,
    regexp: Any,
) -> None:
    """При `off` (умолчание) поведение прежнее: эхо становится строкой, а SELECT
    исходящих не выполняется вовсе — ветка стоит цену только включённой."""
    await apply_inbound_event(
        db, redis, account, история._live("Привезу сам, куда?", msg="m1", when=T0)
    )
    await _исходящее(db_sessionmaker, "Наш адрес: ул Ленина 5, офис 3", T0 + timedelta(minutes=1))
    запросы = _шпион_execute(monkeypatch, db)
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(
            db,
            redis,
            account,
            история._live("Ленина 5, подъеду", msg="m2", when=T0 + timedelta(minutes=2)),
        )
    (строка,) = await _строки(db_sessionmaker)
    assert строка.value == "Ленина, 5" and not (строка.trace or {}).get("stop_shadow")
    assert not [q for q in запросы if "<regexp>" in q], "при off исходящие не читаются"
    assert not _эхо_в_журнале(логи)


async def test_on_исходящая_раньше_клиента_строки_нет_и_журнал(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: Any,
    без_сети: Any,
    regexp: Any,
) -> None:
    """Наш адрес назван нами первым → клиентский повтор строкой не становится;
    в журнале источник `dialog_echo` и ни слова клиента; исходящие читаются
    одним SELECT; «правила промолчали» — False (модель эхо не допишет)."""
    await _настройка(db_sessionmaker, **{app_settings.ADDRESS_PARSE_RULES: f"{ПРАВИЛО}=on"})
    await apply_inbound_event(
        db, redis, account, история._live("Привезу сам, куда?", msg="m1", when=T0)
    )
    await _исходящее(db_sessionmaker, "Наш адрес: ул Ленина 5, офис 3", T0 + timedelta(minutes=1))
    запросы = _шпион_execute(monkeypatch, db)
    результат: dict[str, Any] = {}
    исходное = inbound_svc._maybe_extract_address

    async def подсмотреть(*a: Any, **kw: Any) -> Any:
        итог = await исходное(*a, **kw)
        результат["итог"] = итог
        return итог

    monkeypatch.setattr(inbound_svc, "_maybe_extract_address", подсмотреть)
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(
            db,
            redis,
            account,
            история._live("Ленина 5, подъеду", msg="m2", when=T0 + timedelta(minutes=2)),
        )
    assert await _строки(db_sessionmaker) == []
    assert результат["итог"] == (None, (), False)
    (запись,) = _эхо_в_журнале(логи)
    assert (запись["source"], запись["state"]) == ("dialog_echo", "on")
    assert запись["level"] in ("B", "C")  # «Наш адрес: …» — вопрос об адресе для ленты
    assert "Ленина" not in str(запись) and "conversation_id" in запись
    assert len([q for q in запросы if "<regexp>" in q]) == 1, "один SELECT исходящих на реплику"
    # Карточка пуста: в неё не попало ничего.
    async with db_sessionmaker() as s:
        client = (await s.execute(sa.select(Client))).scalars().one()
    assert client.address is None and client.address_candidate_id is None


async def test_клиент_назвал_раньше_исходящей_строка_есть(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, без_сети: Any, regexp: Any
) -> None:
    """Оператор переспросил «ул. Ленина 5, верно?» после того, как клиент
    написал «да это дом 5 по ленина» (разбор такое не берёт — упоминание по
    тексту): первое упоминание клиентское, сторож молчит, строка есть."""
    await _настройка(db_sessionmaker, **{app_settings.ADDRESS_PARSE_RULES: f"{ПРАВИЛО}=on"})
    await apply_inbound_event(db, redis, account, история._live("Здравствуйте", msg="m1", when=T0))
    await apply_inbound_event(
        db,
        redis,
        account,
        история._live("да это дом 5 по ленина", msg="m2", when=T0 + timedelta(minutes=1)),
    )
    assert await _строки(db_sessionmaker) == [], "стенд: неразобранная реплика строки не даёт"
    await _исходящее(db_sessionmaker, "ул. Ленина 5, верно?", T0 + timedelta(minutes=2))
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(
            db,
            redis,
            account,
            история._live("Ленина 5, подъезд 2", msg="m3", when=T0 + timedelta(minutes=3)),
        )
    (строка,) = await _строки(db_sessionmaker)
    assert строка.value == "Ленина, 5"
    assert not _эхо_в_журнале(логи)


async def test_список_владельца_и_тень(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: Any,
    без_сети: Any,
    regexp: Any,
) -> None:
    """(а) Адрес из списка владельца при `on` — строки нет, `source=own_list`,
    и исходящие не читаются (список дешевле SELECT'а); (б) при `shadow` адрес
    из вывода задачи — строка есть, со следом `stop_shadow=workshop_echo`."""
    await _настройка(
        db_sessionmaker,
        **{
            app_settings.ADDRESS_PARSE_RULES: f"{ПРАВИЛО}=on",
            app_settings.ADDRESS_OWN_ADDRESSES: "ул Ленина 5",
        },
    )
    await apply_inbound_event(db, redis, account, история._live("Здравствуйте", msg="m1", when=T0))
    запросы = _шпион_execute(monkeypatch, db)
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(
            db,
            redis,
            account,
            история._live("Ленина 5, подъеду", msg="m2", when=T0 + timedelta(minutes=1)),
        )
    assert await _строки(db_sessionmaker) == []
    (запись,) = _эхо_в_журнале(логи)
    assert (запись["source"], запись["state"]) == ("own_list", "on")
    assert not [q for q in запросы if "<regexp>" in q]
    # (б) тень: вывод задачи, формат «улица, дом; …».
    await _настройка(
        db_sessionmaker,
        **{
            app_settings.ADDRESS_PARSE_RULES: f"{ПРАВИЛО}=shadow",
            app_settings.ADDRESS_OWN_ADDRESSES_AUTO: "ул. Мира, 10; пр. Победы, 7а",
        },
    )
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(
            db,
            redis,
            account,
            история._live("Мира 10, жду", msg="m3", when=T0 + timedelta(minutes=2)),
        )
    (строка,) = await _строки(db_sessionmaker)
    assert строка.value == "Мира, 10"
    assert строка.trace is not None and строка.trace.get("stop_shadow") == ПРАВИЛО
    (запись,) = _эхо_в_журнале(логи)
    assert (запись["source"], запись["state"]) == ("own_list", "shadow")


# ── задача: реестр своих адресов из исходящих ─────────────────────────────────


async def _диалог(
    s: Any, account_id: uuid.UUID, реплики: list[tuple[str, str, datetime]]
) -> Conversation:
    """Диалог с репликами `(направление, текст, когда)`; `out` — оператор."""
    client = Client(channel="avito", external_id=uuid.uuid4().hex[:10], name="Клиент")
    s.add(client)
    await s.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=uuid.uuid4().hex[:12],
        account_id=account_id,
        client_id=client.id,
        status="new",
        last_message_at=реплики[-1][2],
    )
    s.add(conv)
    await s.flush()
    for направление, текст, когда in реплики:
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=uuid.uuid4().hex[:12],
                direction=направление,
                sender_type="client" if направление == "in" else "operator",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=когда,
            )
        )
    await s.flush()
    return conv


@pytest.fixture
def оснастка_задачи(monkeypatch: Any, db_sessionmaker: Any) -> None:
    monkeypatch.setattr(job.db_mod, "session_scope", db_sessionmaker)


async def _журнал_задачи(db_sessionmaker: Any) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        строки = (
            await s.execute(
                sa.select(AuditLog)
                .where(AuditLog.action == job.SETTINGS_ACTION)
                .order_by(AuditLog.created_at)
            )
        ).scalars()
        return [r for r in строки if (r.details or {}).get("source") == job.SOURCE]


async def test_задача_собирает_реестр_и_пишет_настройку(
    db_sessionmaker: Any, make_avito_account: Any, оснастка_задачи: Any, regexp: Any
) -> None:
    """«ул Мира 10» в исходящих четырёх диалогов, но в одном клиент назвал дом
    раньше — засчитаны три; «Ленина 7» — по одному диалогу у двух аккаунтов.
    Настройка + журнал; повтор без изменений — без записи."""
    a = await make_avito_account(история.ACCOUNT_UID)
    b = await make_avito_account(история.ACCOUNT_UID + 1)
    м = timedelta(minutes=1)
    ч = timedelta(hours=1)  # диалоги в разные часы: образец — из первой по времени исходящей
    async with db_sessionmaker() as s:
        for i in range(2):
            await _диалог(
                s,
                a.id,
                [
                    ("in", "Привезу сам, куда?", T0 + i * ч),
                    ("out", "Наш адрес: ул Мира 10, вход со двора", T0 + i * ч + м),
                    ("in", "Мира 10, подъеду", T0 + i * ч + 2 * м),
                ],
            )
        # Клиент назвал дом раньше нас — повтор оператора не в счёт.
        await _диалог(
            s,
            a.id,
            [
                ("in", "да это дом 10 по мира", T0 + 2 * ч),
                ("out", "ул. Мира 10, верно?", T0 + 2 * ч + м),
            ],
        )
        await _диалог(
            s, a.id, [("in", "Где вы?", T0 + 3 * ч), ("out", "ул Мира 10", T0 + 3 * ч + м)]
        )
        # Сито исходящих — `address_funnel.ADDRESS_LIKE_PATTERN`: «проспект» в
        # нём есть, «пр.» нет — форма без типа из списка задачей не читается.
        await _диалог(
            s,
            b.id,
            [("in", "Куда везти?", T0 + 4 * ч), ("out", "проспект Ленина 7", T0 + 4 * ч + м)],
        )
        await _диалог(
            s,
            a.id,
            [("in", "Куда везти?", T0 + 5 * ч), ("out", "проспект Ленина, 7", T0 + 5 * ч + м)],
        )
        await s.commit()
    now = T0 + timedelta(days=1)
    async with db_sessionmaker() as s:
        реестр = await job.collect_own_addresses(s, now=now)
    assert реестр == {
        (("мира",), "10"): {"conversations": 3, "accounts": 1, "sample": "ул Мира, 10"},
        (("ленина",), "7"): {"conversations": 2, "accounts": 2, "sample": "проспект Ленина, 7"},
    }
    # Окно: те же исходящие старше 90 дней — реестр пуст.
    async with db_sessionmaker() as s:
        assert await job.collect_own_addresses(s, now=now + timedelta(days=91)) == {}

    with structlog.testing.capture_logs() as логи:
        итог = await job.run_weekly(now=now)
    assert set(итог) == set(реестр)
    async with db_sessionmaker() as s:
        assert (
            await app_settings.get(s, app_settings.ADDRESS_OWN_ADDRESSES_AUTO)
            == "ул Мира, 10; проспект Ленина, 7"
        )
    (запись,) = await _журнал_задачи(db_sessionmaker)
    assert запись.user_id is None and запись.details["keys"] == 2
    assert запись.details["before"] == {"own_addresses_auto": ""}
    assert запись.details["after"] == {"own_addresses_auto": "ул Мира, 10; проспект Ленина, 7"}
    (лог,) = [л for л in логи if л["event"] == "address_own.collected"]
    assert (лог["keys"], лог["changed"], лог["scanned"]) == (2, True, 6)
    # Повтор без изменений — настройка и журнал не трогаются.
    with structlog.testing.capture_logs() as логи:
        await job.run_weekly(now=now)
    assert len(await _журнал_задачи(db_sessionmaker)) == 1
    (лог,) = [л for л in логи if л["event"] == "address_own.collected"]
    assert лог["changed"] is False
    # Вывод задачи читается тем же списком, что и список владельца.
    assert address_own.own_keys("", "ул Мира, 10; проспект Ленина, 7") == set(реестр)


async def test_порции_по_паре_created_at_id_и_потолок(
    db_sessionmaker: Any, make_avito_account: Any, monkeypatch: Any, regexp: Any
) -> None:
    """Порция по `(created_at, id)`: три исходящих в одну и ту же секунду на
    границе порции не теряются и не читаются дважды; потолок останавливает
    проход с предупреждением."""
    a = await make_avito_account(история.ACCOUNT_UID)
    monkeypatch.setattr(job, "BATCH", 2)
    async with db_sessionmaker() as s:
        conv = await _диалог(s, a.id, [("in", "Где вы?", T0)])
        for i in range(7):
            # 0,1,1,1,2,2,3 — совпадающие метки времени.
            when = T0 + timedelta(minutes=(0, 1, 1, 1, 2, 2, 3)[i])
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"o{i}",
                    direction="out",
                    sender_type="operator",
                    body=f"ул Мира {i + 1}",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=when,
                )
            )
        await s.commit()
    now = T0 + timedelta(hours=1)
    async with db_sessionmaker() as s:
        по_ключу, просмотрено = await job._исходящие(s, since=T0 - timedelta(days=1), now=now)
    assert просмотрено == 7
    assert {ключ[1] for ключ in по_ключу} == {str(i) for i in range(1, 8)}
    monkeypatch.setattr(job, "MAX_ROWS", 4)
    with structlog.testing.capture_logs() as логи:
        async with db_sessionmaker() as s:
            _, просмотрено = await job._исходящие(s, since=T0 - timedelta(days=1), now=now)
    assert просмотрено == 4
    (лог,) = [л for л in логи if л["event"] == "address_own.scan_capped"]
    assert лог["scanned"] == 4 and лог["max_rows"] == 4


def test_текст_реестра_по_убыванию_диалогов_и_под_потолок() -> None:
    реестр: dict[Any, dict[str, Any]] = {
        (("мира",), "10"): {"conversations": 3, "accounts": 1, "sample": "ул Мира, 10"},
        (("ленина",), "7"): {"conversations": 5, "accounts": 2, "sample": "пр. Ленина, 7"},
        (("садовая",), "3"): {"conversations": 3, "accounts": 1, "sample": "Садовая, 3"},
    }
    assert job.registry_text(реестр) == "пр. Ленина, 7; Садовая, 3; ул Мира, 10"
    assert job.registry_text({}) == ""
    # Образец, который не читается обратно в свой ключ, в настройку не идёт.
    with structlog.testing.capture_logs() as логи:
        assert (
            job.registry_text(
                {(("мира",), "10"): {"conversations": 3, "accounts": 1, "sample": "мусор"}}
            )
            == ""
        )
    assert [л["event"] for л in логи] == ["address_own.sample_unreadable"]


def test_задача_зарегистрирована_в_планировщике() -> None:
    """Задача ищется в собранном планировщике, а не в тексте исходника."""
    from app.scheduler.main import build_scheduler

    задача = build_scheduler().get_job(job.JOB_ID)
    assert задача is not None and задача.func is job.run_weekly
    assert str(задача.trigger.fields[4]) == "mon"
    assert (str(задача.trigger.fields[5]), str(задача.trigger.fields[6])) == ("1", "10")


# ── ручка настроек ────────────────────────────────────────────────────────────


async def test_ручка_own_addresses_мусор_400_с_текстом_части(
    client: Any, tokens: Any, db_sessionmaker: Any
) -> None:
    url = "/api/v1/settings/address-detect"
    res = await client.patch(
        url, json={"own_addresses": "ул Ленина 5; мусор"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 400, res.text
    assert "мусор" in res.text and "address_own_addresses" in res.text
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_OWN_ADDRESSES) == ""
    res = await client.patch(
        url, json={"own_addresses": " ул Ленина 5, Мира, 10 "}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200, res.text
    assert res.json()["own_addresses"] == "ул Ленина 5, Мира, 10"
    assert res.json()["own_addresses_auto"] == ""
    # Вывод задачи в теле не принимается: поле только на показ.
    res = await client.patch(
        url, json={"own_addresses_auto": "ул Невская, 7а"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200 and res.json()["own_addresses_auto"] == ""
    res = await client.get(url, headers=hdr(tokens["admin"]))
    assert (res.json()["own_addresses"], res.json()["own_addresses_auto"]) == (
        "ул Ленина 5, Мира, 10",
        "",
    )


def test_событие_в_реестре_и_умолчание_off() -> None:
    """Имя правила в реестре разбора с умолчанием `off`; настройка принимает
    его и отвергает чужое."""
    assert address_parse.rule_state(None, ПРАВИЛО) == address_parse.PARSE_OFF
    assert address_parse.rules_from_setting(f"{ПРАВИЛО}=shadow")[ПРАВИЛО] == "shadow"
    with pytest.raises(ValueError):
        address_parse.rules_from_setting("workshop_eho=on")
    found = address_parse.parse("Ленина 5, подъеду")
    assert found is not None
    тень = address_parse.under_rule({ПРАВИЛО: "shadow"}, ПРАВИЛО, old=found, new=None)
    assert тень is not None and тень.trace == {"stop_shadow": ПРАВИЛО}
    assert dataclasses.replace(тень, trace=None) == found
