"""Пакет 6.0а, разбор и дверь (20.09): реестр правил разбора, проводка
политики, историческая дверь (I-2 для слоя разбора, I-10).

Программа «автоматически и точно» §0.3/§1.1:

* `address_parse.PARSE_RULES` — реестр правил СЛОЯ РАЗБОРА (`on|off|shadow`),
  умолчание нового имени `off`; настройка `address_parse.rules` перекрывает
  поимённо, неизвестное имя или значение — ошибка валидации (400 у ручки);
* `parse(..., rules=…)` и все входы разбора (`gate`, формы города, место,
  части) получают политику; пока реестр пуст — поведение как сегодня;
* `under_rule` — единственное место, где политика разбора становится
  поведением: `on` — новый разбор, `off` — старый, `shadow` — старый в бой,
  разница — `trace.parse_shadow`/`trace.stop_shadow` (`Found.trace` →
  `clients.след_разбора` → колонка `trace`), тень без строк;
* `inbound` читает политику один раз на входящее (`parse_rules`) и передаёт
  во все разборы реплики; `разбор_реплики_сейчас` и ворота модели
  (`address_llm.looks_like_address`) — под той же политикой;
* I-10: историческая дверь (`avito_accounts._insert_history_message`) кладёт
  служебные записи Авито («[Системное сообщение] …») той же парой
  `system/avito`, что живой путь, одним признаком `inbound.avito_system_prefixed`;
  геоточка истории (`is_system` у адаптера истории) остаётся репликой клиента.

ДИВЕРСИИ (каждая обязана краснеть; прогнаны 20.09 «правка → тест → откат»):
снять проверку имени в `rules_from_setting` — `test_rules_неизвестное_имя_и_значение`;
`under_rule` в тени возвращает новый разбор — `test_under_rule_тень_старый_в_бой_разница_в_след`;
не сравнивать пункт при одинаковом ключе — тот же тест (`changed=["settlement"]`);
`след_разбора` без `found.trace` — `test_след_разбора_доносит_ключи_тени`;
убрать `rules=rules` у любого разбора в `_maybe_extract_address` —
`test_политика_один_раз_на_входящее_и_во_все_разборы`; `разбор_реплики_сейчас`
без чтения настройки — `test_разбор_реплики_сейчас_под_той_же_политикой`;
`looks_like_address` без `rules` — `test_ворота_модели_под_той_же_политикой`;
дверь истории снова кладёт приставку как `in/client` —
`test_историческая_дверь_кладёт_служебную_запись_как_живой_путь`; дверь судит
по `is_system` вместо приставки — `test_геоточка_истории_остаётся_репликой_клиента`.

Тексты вымышленные, номер — +7 900 111-22-44, имён нет.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.models import Client, Conversation, Message
from app.services import address_ask, address_llm, address_parse, app_settings
from app.services import clients as clients_svc
from app.services import inbound as inbound_svc
from app.services.address_parse import Found
from app.services.inbound import apply_inbound_event
from tests.unit import test_paket2_history_1909 as история

pytestmark = pytest.mark.anyio

# Фикстура и стенд соседнего файла — присваиванием, не импортом имени (ruff F811).
без_сети = история.без_сети
NOW = datetime.now(UTC).replace(microsecond=0)
ПРАВИЛО = "q_test_rule"
СЛУЖЕБНОЕ = "[Системное сообщение] Ассистент Авито ответил на вопрос клиента"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(история.ACCOUNT_UID)


@pytest.fixture
def правило(monkeypatch: pytest.MonkeyPatch) -> str:
    """Временное правило разбора в реестре — с умолчанием `off`, как рождается
    любое новое имя (§0.3)."""
    monkeypatch.setitem(address_parse.PARSE_RULES, ПРАВИЛО, address_parse.PARSE_OFF)
    return ПРАВИЛО


def _found(street: str = "ул. Ленина", house: str = "5", **kw: Any) -> Found:
    return Found(
        street=street,
        house=house,
        raw=f"{street} {house}",
        start=0,
        end=len(street) + len(house) + 1,
        level=address_parse.LEVEL_A,
        **kw,
    )


# ── реестр и настройка ────────────────────────────────────────────────────────


def test_реестр_пуст_поведение_как_сегодня() -> None:
    """Пока правил в реестре нет, политика ничего не меняет: разбор с `rules`
    из настройки (пустой), с пустым словарём и без него — один и тот же."""
    assert all(v in address_parse.PARSE_POLICIES for v in address_parse.PARSE_RULES.values())
    assert address_parse.rules_from_setting("") == dict(address_parse.PARSE_RULES)
    assert address_parse.rules_from_setting(None) == dict(address_parse.PARSE_RULES)
    корпус = [
        "ул. Ленина 5 кв 3",
        "Приезжайте на Садовая 12, 2 подъезд",
        "Телевизор Самсунг 55 не включается",
        "д. Большая Дубрава, массив Южный",
        "Ангарск 86-11, кв 5",
        "38/07 кв 3",
        "кв 7, домофон 12",
        "",
    ]
    for текст in корпус:
        for rules in ({}, address_parse.rules_from_setting("")):
            assert address_parse.parse(текст, rules=rules) == address_parse.parse(текст), текст
            assert address_parse.parse(текст, про_адрес=True, rules=rules) == address_parse.parse(
                текст, про_адрес=True
            ), текст
            assert address_parse.gate(текст, rules=rules) == address_parse.gate(текст), текст
            assert address_parse.parse_place(текст, rules=rules) == address_parse.parse_place(
                текст
            ), текст
            assert address_parse.parts_only(текст, rules=rules) == address_parse.parts_only(
                текст
            ), текст
            for город in ("Ангарск", "Зеленоград", "Набережные Челны", "Калуга", None):
                assert address_parse.parse_by_city(
                    текст, город, rules=rules
                ) == address_parse.parse_by_city(текст, город), (текст, город)


def test_rules_неизвестное_имя_и_значение(правило: str) -> None:
    """Опечатка в настройке не имеет права молча включить не то правило:
    неизвестное имя и неизвестное значение — ошибка валидации; годное —
    поверх умолчания реестра."""
    # Поверх ВСЕГО реестра: с пакета 7a в нём живут настоящие имена, и точный
    # литерал из одного временного правила краснел бы у каждого нового.
    реестр = dict(address_parse.PARSE_RULES)
    assert address_parse.rules_from_setting(f"{правило}=on") == {**реестр, правило: "on"}
    assert address_parse.rules_from_setting(f" {правило} = Shadow ;\n") == {
        **реестр,
        правило: "shadow",
    }
    assert address_parse.rules_from_setting("") == {**реестр, правило: "off"}
    with pytest.raises(ValueError, match="неизвестное правило разбора"):
        address_parse.rules_from_setting(f"{правило}=on, STOP_NOPE=off")
    with pytest.raises(ValueError, match="неизвестное значение"):
        address_parse.rules_from_setting(f"{правило}=suggest")
    with pytest.raises(ValueError, match="правило=значение"):
        address_parse.rules_from_setting(правило)
    # Политика правила в разборе: перекрытие → умолчание → `off` вне реестра.
    assert address_parse.rule_state(None, правило) == "off"
    assert address_parse.rule_state({}, правило) == "off"
    assert address_parse.rule_state({правило: "shadow"}, правило) == "shadow"
    assert address_parse.rule_state({правило: "on"}, "not_registered_yet") == "off"


async def test_настройка_parse_rules_ручка(client: Any, tokens: Any, правило: str) -> None:
    url = "/api/v1/settings/address-detect"
    плохо = await client.patch(
        url, headers=hdr(tokens["admin"]), json={"parse_rules": f"{правило}=suggest"}
    )
    assert плохо.status_code == 400, плохо.text
    assert плохо.json()["error"]["details"]["fields"][0]["field"] == "address_parse.rules"
    ок = await client.patch(
        url, headers=hdr(tokens["admin"]), json={"parse_rules": f"{правило}=shadow"}
    )
    assert ок.status_code == 200, ок.text
    assert ок.json()["parse_rules"] == f"{правило}=shadow"
    ок = await client.patch(url, headers=hdr(tokens["admin"]), json={"parse_rules": ""})
    assert ок.status_code == 200 and ок.json()["parse_rules"] == ""


# ── under_rule: политика разбора → поведение ─────────────────────────────────


def test_under_rule_on_и_off(правило: str) -> None:
    старый, новый = _found(), _found(house="5 корпус 2")
    assert address_parse.under_rule({правило: "on"}, правило, old=старый, new=новый) is новый
    assert address_parse.under_rule({правило: "on"}, правило, old=старый, new=None) is None
    assert address_parse.under_rule({правило: "off"}, правило, old=старый, new=новый) is старый
    assert address_parse.under_rule(None, правило, old=старый, new=новый) is старый
    assert address_parse.under_rule({}, правило, old=None, new=новый) is None
    # Вне реестра — `off` по построению, что бы ни лежало в словаре.
    assert address_parse.under_rule({"x": "on"}, "x", old=старый, new=новый) is старый


def test_under_rule_тень_старый_в_бой_разница_в_след(правило: str) -> None:
    rules = {правило: "shadow"}
    старый = _found()
    # Другой ключ: в бой — старый, разница — следом на нём (копия, не правка).
    итог = address_parse.under_rule(rules, правило, old=старый, new=_found(house="5 корпус 2"))
    assert итог is not None and итог is not старый and итог.value == старый.value
    assert старый.trace is None
    assert итог.trace == {
        "parse_shadow": {
            "rule": правило,
            "key_old": "ул. Ленина, 5",
            "key_new": "ул. Ленина, 5 корпус 2",
            "changed": ["value"],
        }
    }
    # Тот же ключ, другой пункт (расклейка Q17) — тоже разница.
    итог = address_parse.under_rule(
        rules, правило, old=старый, new=_found(settlement="Селты", settlement_type="село")
    )
    assert итог is not None and итог.settlement is None
    assert итог.trace is not None
    assert итог.trace["parse_shadow"]["changed"] == ["settlement", "settlement_type"]
    # Стоп-класс снял бы строку — строка остаётся, след `stop_shadow`.
    итог = address_parse.under_rule(rules, правило, old=старый, new=None)
    assert (
        итог is not None and итог.value == старый.value and итог.trace == {"stop_shadow": правило}
    )
    # След дописывается поверх прежнего, прежние ключи не теряются.
    с_формой = dataclasses.replace(старый, trace={"form": "F1"})
    итог = address_parse.under_rule(rules, правило, old=с_формой, new=None)
    assert итог is not None and итог.trace == {"form": "F1", "stop_shadow": правило}
    # Одинаковые поля строки — не разница: следа нет, объект тот же.
    assert address_parse.under_rule(rules, правило, old=старый, new=_found()) is старый
    # Тень нашла бы адрес там, где старый молчит: строки нет, только журнал
    # без слов клиента (К-5).
    with structlog.testing.capture_logs() as логи:
        assert address_parse.under_rule(rules, правило, old=None, new=_found()) is None
        assert address_parse.under_rule(rules, правило, old=None, new=None) is None
    (запись,) = [л for л in логи if л["event"] == "address.parse_shadow"]
    assert (запись["rule"], запись["status"], запись["level"]) == (правило, "would_add", "A")
    assert "Ленина" not in str(запись)


def test_след_разбора_доносит_ключи_тени() -> None:
    """`Found.trace` едет в колонку `trace` тем же словарём, что именованные
    ключи разбора; именованные — поверх; пусто — None (SQL NULL)."""
    found = _found(trace={"stop_shadow": ПРАВИЛО, "form": "F2"}, parse_form="split_head")
    assert clients_svc.след_разбора(found) == {
        "stop_shadow": ПРАВИЛО,
        "form": "F2",
        "parse_form": "split_head",
    }
    assert clients_svc.след_разбора(_found()) is None
    assert clients_svc.след_разбора(_found(trace={})) is None
    assert clients_svc.след_разбора(_found(trace={"form": ""})) is None


# ── проводка: один раз на входящее, во все разборы ───────────────────────────


def _шпион_разбора(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Каждый вход разбора, который зовёт `inbound`, записывает переданную
    политику; сам разбор — как был."""
    вызовы: list[tuple[str, Any]] = []
    for имя in ("parse", "parse_by_city", "parse_place", "parts_only"):
        исходный = getattr(address_parse, имя)

        def обёртка(*args: Any, _имя: str = имя, _исходный: Any = исходный, **kwargs: Any) -> Any:
            вызовы.append((_имя, kwargs.get("rules", "НЕТ")))
            return _исходный(*args, **kwargs)

        monkeypatch.setattr(address_parse, имя, обёртка)
    return вызовы


async def _политика_в_базе(db_sessionmaker: Any, текст: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_PARSE_RULES: текст}, user_id=None)
        await s.commit()


def _из_настройки(правило: str, значение: str) -> dict[str, str]:
    """Политика, какой её читает `inbound.parse_rules` из настройки: весь реестр
    (с пакета 7a в нём настоящие имена) и перекрытие временного правила."""
    return {**address_parse.PARSE_RULES, правило: значение}


async def test_политика_один_раз_на_входящее_и_во_все_разборы(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    правило: str,
    без_сети: Any,
) -> None:
    await _политика_в_базе(db_sessionmaker, f"{правило}=shadow")
    чтений: list[int] = []
    исходное = inbound_svc.parse_rules

    async def считающее(db_: Any) -> dict[str, str]:
        чтений.append(1)
        return await исходное(db_)

    monkeypatch.setattr(inbound_svc, "parse_rules", считающее)
    вызовы = _шпион_разбора(monkeypatch)
    # Реплика без головы и без адреса: живой путь проходит все ветки — разбор,
    # формы города, место, пункт из контекста, части, — и каждая обязана
    # получить ту же политику.
    await apply_inbound_event(
        db, redis, account, история._live("кв 7, второй подъезд", msg="m-1", when=NOW)
    )
    assert вызовы, "разбор не звался — стенд пуст"
    assert {имя for имя, _ in вызовы} >= {"parse", "parse_place", "parts_only"}
    мимо = [(имя, r) for имя, r in вызовы if r != _из_настройки(правило, "shadow")]
    assert not мимо, f"разбор без политики входящего: {мимо}"
    assert len(чтений) == 1, "политика читается один раз на входящее"
    # Строка записана под старым разбором — реестр правил без поведения.
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "chat-n29")
            )
        ).scalar_one()
        client = await s.get(Client, conv.client_id)
    assert client is not None and client.address is None
    # Форма города: «86-11, кв 5» в Ангарске идёт через `parse_by_city`, а
    # части за парой — через внутренний `parts_only`; контракт §5.2 требует,
    # чтобы и внутренние рекурсии шли под политикой входящего (ревью 20.09,
    # #12) — первая реплика до веток пар не доходит и этого не ловила.
    вызовы.clear()
    в_ангарске = dataclasses.replace(
        история._live("86-11, кв 5", msg="m-ang", when=NOW, chat="chat-ang"),
        item_url="https://www.avito.ru/angarsk/predlozheniya_uslug/remont_123456789",
    )
    await apply_inbound_event(db, redis, account, в_ангарске)
    assert {имя for имя, _ in вызовы} >= {"parse", "parse_by_city", "parts_only"}, вызовы
    мимо = [(имя, r) for имя, r in вызовы if r != _из_настройки(правило, "shadow")]
    assert not мимо, f"форма города без политики входящего: {мимо}"
    assert len(чтений) == 2, "второе входящее — второе чтение, не больше"


async def test_разбор_реплики_сейчас_под_той_же_политикой(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    правило: str,
    без_сети: Any,
) -> None:
    """Догон и сторож автозаписи судят реплику той же политикой, что живой
    путь (класс dva-puti-raznyi-schet): без `rules` — из настройки, с `rules`
    — как передали, без второго чтения."""
    await apply_inbound_event(db, redis, account, история._live("ул. Мира 7", msg="m-1", when=NOW))
    await _политика_в_базе(db_sessionmaker, f"{правило}=on")
    вызовы = _шпион_разбора(monkeypatch)
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "chat-n29")
            )
        ).scalar_one()
        msg = (
            await s.execute(sa.select(Message).where(Message.conversation_id == conv.id).limit(1))
        ).scalar_one()
        found = await inbound_svc.разбор_реплики_сейчас(s, conv, msg, client_id=conv.client_id)
        assert found is not None and found.value == "ул. Мира, 7"
        assert вызовы and all(r == _из_настройки(правило, "on") for _, r in вызовы), вызовы
        вызовы.clear()
        found = await inbound_svc.разбор_реплики_сейчас(
            s, conv, msg, client_id=conv.client_id, rules={правило: "off"}
        )
        assert found is not None and all(r == {правило: "off"} for _, r in вызовы)


async def test_догон_читает_политику_один_раз_на_прогон(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    правило: str,
) -> None:
    """`address-reparse` читает настройку правил ОДИН раз на прогон и передаёт
    её в оба обхода — нерешённых строк и `--auto` — и в разбор места по
    отклонённой карте (ревью 20.09, #13): вне `one_pass` каждое чтение — SELECT,
    и по тысячам строк догон слал бы тысячи лишних запросов."""
    from app.cli import run_address_reparse
    from app.services import geocode as g
    from tests.unit import test_paket3_1909 as пакет3

    # Две строки для основного обхода (нерешённая с адресом и нерешённая речь с
    # отказом карты — она доходит до `parse_place`) и одна автопринятая для `--auto`.
    await пакет3._речь_в_карточке(seed_conversation, db_sessionmaker)
    await пакет3._строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="спальное место 200",
        found=Found(street="место", house="200", raw="место 200", start=8, end=17, level="C"),
        geo_status=g.GEO_NOT_FOUND,
    )
    await _политика_в_базе(db_sessionmaker, f"{правило}=shadow")
    чтений: list[int] = []
    исходное = inbound_svc.parse_rules

    async def считающее(db_: Any) -> dict[str, str]:
        чтений.append(1)
        return await исходное(db_)

    monkeypatch.setattr(inbound_svc, "parse_rules", считающее)
    разборы: list[Any] = []
    исходный_разбор = inbound_svc.разбор_реплики_сейчас

    async def разбор(*args: Any, **kwargs: Any) -> Any:
        разборы.append(kwargs.get("rules", "НЕТ"))
        return await исходный_разбор(*args, **kwargs)

    monkeypatch.setattr(inbound_svc, "разбор_реплики_сейчас", разбор)
    вызовы = _шпион_разбора(monkeypatch)
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=True, auto=True, redis=redis)
    assert len(разборы) == 3, "две нерешённые строки и одна автопринятая"
    assert all(r == _из_настройки(правило, "shadow") for r in разборы), разборы
    assert ("parse_place", _из_настройки(правило, "shadow")) in вызовы, (
        "место по отказу карты — под политикой"
    )
    мимо = [(имя, r) for имя, r in вызовы if r != _из_настройки(правило, "shadow")]
    assert not мимо, f"разбор догона без политики прогона: {мимо}"
    assert len(чтений) == 1, "политика читается один раз на прогон, не на строку"


async def test_ворота_модели_под_той_же_политикой(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    правило: str,
    без_сети: Any,
) -> None:
    """`looks_like_address` читает невод под политикой разбора (М-10), и ворота
    `llm_read_wanted` передают ему политику входящего."""
    ворота: list[Any] = []
    исходный_gate = address_parse.gate

    def gate(*args: Any, **kwargs: Any) -> Any:
        ворота.append(kwargs.get("rules", "НЕТ"))
        return исходный_gate(*args, **kwargs)

    monkeypatch.setattr(address_parse, "gate", gate)
    assert address_llm.looks_like_address("ул. Ленина 5", rules={правило: "shadow"}) is True
    assert ворота == [{правило: "shadow"}]
    await apply_inbound_event(
        db, redis, account, история._live("здравствуйте", msg="m-0", when=NOW)
    )
    await _политика_в_базе(db_sessionmaker, f"{правило}=shadow")
    monkeypatch.setattr(address_llm, "enabled", lambda: True)
    ворота.clear()
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "chat-n29")
            )
        ).scalar_one()
        assert (
            await inbound_svc.llm_read_wanted(
                s,
                conv,
                речь="Пушки на 10",
                правила_промолчали=True,
                причина_адреса=None,
                before=NOW,
            )
            is False
        )
    assert ворота == [_из_настройки(правило, "shadow")]


# ── I-10: историческая дверь и служебные записи Авито ─────────────────────────


def test_признак_приставки_один_на_оба_пути() -> None:
    assert inbound_svc.avito_system_prefixed(СЛУЖЕБНОЕ) is True
    assert inbound_svc.avito_system_prefixed("[Системное сообщение]") is True
    assert inbound_svc.avito_system_prefixed("Здравствуйте, ул. Ленина 5") is False
    assert inbound_svc.avito_system_prefixed(" [Системное сообщение] с пробелом") is False
    assert inbound_svc.avito_system_prefixed(None) is False
    assert inbound_svc.avito_system_prefixed("") is False


async def _лента(db_sessionmaker: Any, chat_id: str) -> dict[str, tuple[str, str]]:
    conv = await история._conv(db_sessionmaker, chat_id)
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(sa.select(Message).where(Message.conversation_id == conv.id))
        ).scalars()
        return {r.external_message_id: (r.direction, r.sender_type) for r in rows}


async def test_историческая_дверь_кладёт_служебную_запись_как_живой_путь(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    без_сети: Any,
) -> None:
    """Одна и та же запись Авито лежит в ленте одной парой, какой дверью ни
    пришла: живой путь — `system/avito`, историческая дверь — то же самое;
    реплика клиента и наше эхо — как раньше. Стенд адресов и
    `client_described` (`inbound_rows`: `direction='in'`) служебную запись
    больше не считают словами клиента."""
    T = NOW
    await apply_inbound_event(
        db, redis, account, история._live("здравствуйте", msg="m-live", when=T)
    )
    await apply_inbound_event(db, redis, account, история._live(СЛУЖЕБНОЕ, msg="m-sys", when=T))
    история_чата = [
        история._raw_msg("m-live", "здравствуйте", created=T),
        история._raw_msg("h-sys", СЛУЖЕБНОЕ, created=T - timedelta(minutes=2)),
        история._raw_msg("h-client", "ул. Мира 7", created=T - timedelta(minutes=3)),
        история._raw_msg(
            "h-own",
            "Куда к вам подъехать?",
            created=T - timedelta(minutes=4),
            author_id=история.ACCOUNT_UID,
        ),
    ]
    await история._догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-n29", история_чата)
    лента = await _лента(db_sessionmaker, "chat-n29")
    assert лента["m-sys"] == ("system", "avito"), "живой путь"
    assert лента["h-sys"] == лента["m-sys"], "дверь истории кладёт ту же пару, что живой путь"
    assert лента["h-client"] == ("in", "client") and лента["h-own"] == ("out", "operator")
    conv = await история._conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        входящие = await address_ask.inbound_rows(
            s, conversation_id=conv.id, since=T - timedelta(hours=1), before=T
        )
    assert sorted((r.body or "") for r in входящие) == ["здравствуйте", "ул. Мира 7"]
    описал, знаков = address_ask.client_described(входящие, min_chars=20)
    assert (описал, знаков) == (False, 19), "служебная запись — не буквы клиента"


async def test_геоточка_истории_остаётся_репликой_клиента(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    без_сети: Any,
) -> None:
    """У адаптера истории `is_system` носит и геоточка с адресом
    (`_SYSTEM_SOURCE_TYPES`): дверь судит по приставке, а не по конверту —
    иначе точка клиента легла бы серым чипом и догон её не увидел бы."""
    T = NOW
    await apply_inbound_event(
        db, redis, account, история._live("когда приедете?", msg="m-live", when=T)
    )
    история_чата = [
        история._raw_msg("m-live", "когда приедете?", created=T),
        история._raw_geo("h-geo", created=T - timedelta(minutes=5)),
    ]
    await история._догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-n29", история_чата)
    лента = await _лента(db_sessionmaker, "chat-n29")
    assert лента["h-geo"] == ("in", "client")
    conv = await история._conv(db_sessionmaker, "chat-n29")
    assert await redis.exists(f"arq:job:cardcatch:{conv.id}"), "догон по геоточке заказан"


async def test_чат_из_служебных_записей_без_догона_и_без_очереди(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    monkeypatch: pytest.MonkeyPatch,
    без_сети: Any,
) -> None:
    """Служебные записи не считаются словами клиента и в очередь чат не ведут —
    как и до пакета; теперь они и в ленте лежат служебными."""
    история_чата = [
        история._raw_msg("h-sys", СЛУЖЕБНОЕ, created=NOW - timedelta(days=1)),
        история._raw_msg(
            "h-own",
            "Добрый день!",
            created=NOW - timedelta(days=1, minutes=1),
            author_id=история.ACCOUNT_UID,
        ),
    ]
    await история._догрузить(
        monkeypatch, db_sessionmaker, redis, account, "chat-sys", история_чата, unread=True
    )
    conv = await история._conv(db_sessionmaker, "chat-sys")
    assert conv.status == "closed" and conv.unread_count == 0
    assert not await redis.exists(f"arq:job:cardcatch:{conv.id}")
    лента = await _лента(db_sessionmaker, "chat-sys")
    assert лента == {"h-sys": ("system", "avito"), "h-own": ("out", "operator")}


async def test_parse_rules_в_бою_нестрогий_устаревшее_имя_не_роняет_приём(
    db_sessionmaker: Any,
) -> None:
    """Ревью 20.09, #5/#14: читатель настроек отдаёт строку как есть (проверка —
    только на записи), значит имя правила, снятое из реестра следующей
    выкаткой, в таблице остаётся. `inbound.parse_rules` обязан не бросать
    (иначе падал бы разбор КАЖДОГО входящего) и не сбрасывать соседей.
    Диверсия: убрать `strict=False` из `parse_rules` — ValueError."""
    from app.models.app_setting import AppSetting

    async with db_sessionmaker() as s:
        s.add(
            AppSetting(
                key=app_settings.ADDRESS_PARSE_RULES, value="снятое_правило=off, ещё_мусор=on"
            )
        )
        await s.commit()
    async with db_sessionmaker() as s:
        правила = await inbound_svc.parse_rules(s)
    assert правила == dict(address_parse.PARSE_RULES)
    with pytest.raises(ValueError):
        address_parse.rules_from_setting("снятое_правило=off")
