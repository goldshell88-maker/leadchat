"""Пакет 6.0б, раздел C (21.09): `address-reparse --golden` — золотой корпус боя
(программа §1.2 I-6, контракт `contract_60b.md` §C).

Принятые строки-дома A/B за окно — автоматикой и человеком — перечитываются
нынешним разбором сухо; команда печатает ВСЕГДА все ключи с нулями
(`golden строк= авто= руками= ключ_изменился= строки_нет= уровень_упал=
не_судимо= сухой_прогон=True`), до 10 расхождений масками — фатальные (смена
ключа, строки нет) первыми, уровень в остаток — и выходит кодом 1, если
ключ сменился или разбор замолчал. Ключ сравнивается семантически
(`address_parse.same_address`, как при рождении строки), уровень считается
отдельно. `не_судимо` (ревью 21.09, #13) — разбор молчит, но реплика и не
была речью об адресе по единственному предикату `inbound.реплика_без_адреса`
(геоточка Авито, «дом N» к месту прошлой реплики): в ворота не входит.
`--rules имя=on,…` — политика разбора поверх настройки, только в
сухом прогоне; `--golden` несовместим с `--no-dry-run`/`--auto` (код 2).
Ничего не пишет: откат порциями, объекты грузятся порциями заново.

ДИВЕРСИИ (каждая прогнана 21.09 «правка → тест → откат», хеш файла сверен):
сравнивать ключ строкой вместо `same_address` в `_golden` —
`test_golden_ключ_сравнивается_семантически`; убрать `raise typer.Exit(code=1)`
— `test_golden_ключ_изменился_код_1_и_пример_маской`,
`test_golden_строки_нет_код_1`; убрать проверку `golden and (not dry_run or
auto)` — `test_golden_несовместим_с_боевым_прогоном_и_auto`; убрать фильтр
`level in (A, B)` из выборки — `test_golden_выборка_только_принятые_дома_A_B`;
любая правка строки в `_golden` (`row.geo_attempts += 1`, на счёт не влияет)
— `test_golden_ничего_не_пишет_когда_разбор_ходит_в_базу` (сторож
UPDATE/INSERT/DELETE; у реплик A без запросов разбора откат правку скрывает,
поэтому сторож стоит на репликах C); не докладывать `rules_text` в обёртке —
`test_обёртка_address_reparse_несёт_golden_и_rules`; грузить строки один раз
и откатывать порциями по старым объектам — `test_golden_порции_откатом`
(MissingGreenlet); подменить политику консоли вместо наложения поверх
настройки — `test_golden_rules_поверх_настройки_один_раз_на_прогон`;
считать `строки_нет` по голому `found is None` без предиката —
`test_golden_геоточка_авито_не_судится`, `test_golden_дом_к_месту_не_судится`;
один список примеров вместо «фатальные первыми» —
`test_golden_примеры_смены_ключа_не_вытесняются_уровнем`.

`--scope accepted|all` (пакет 7a §B.2, 21.09): `all` берёт все строки-дома за
окно (`pending`/`accepted`/`rejected`, A/B/C, не модель) и печатает сверх
прежних строк `golden scope=all строк=N` и по паре статус/уровень
`строк[s/l]= снято[s/l]= ключ_изменился[s/l]=` (два последних — только
ненулевые) плюс до 10 снятых вне ворот масками отдельным списком; ворота,
первая строка печати и код выхода — по-прежнему только принятые A/B; мусор в
`--scope` и `--scope all` без `--golden` — код 2 до чтения базы.
ДИВЕРСИИ `--scope` (прогнаны 21.09 тем же порядком): выборка `all` с прежним
фильтром `accepted`/A,B — `test_golden_scope_all_считает_снятые_по_статусам`
(нет `scope=all строк=5`); считать снятые вне ворот в `строки_нет`/код выхода
— тот же тест (первая строка и код 0); печатать группы и без `scope=all` —
`test_golden_scope_accepted_печать_как_сегодня`; убрать проверку `scope not
in _GOLDEN_SCOPES` — `test_golden_scope_мусор_и_без_golden_код_2`; не
докладывать `scope` из обёртки — `test_обёртка_address_reparse_несёт_scope`.

Реплики вымышленные, имён и телефонов нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import typer
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import ClientAddressCandidate, Message
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_SOURCE_LLM
from app.services import address_parse, app_settings
from app.services import inbound as inbound_svc
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)
ПРАВИЛО = "q_golden_rule"


def событие(text: str, *, author: int, chat: str) -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat,
        external_message_id=f"m-{uuid.uuid4().hex[:8]}",
        author_id=author,
        account_user_id=111222333,
        text=text,
        created_at=T0,
        client_name="Клиент",
        item_title="Ремонт",
        item_url="https://avito.ru/orsk/item/1",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(111222333)


@pytest.fixture
def правило(monkeypatch: pytest.MonkeyPatch) -> str:
    """Временное правило разбора в реестре — умолчание `off`, как у любого
    нового имени (§0.3)."""
    monkeypatch.setitem(address_parse.PARSE_RULES, ПРАВИЛО, address_parse.PARSE_OFF)
    return ПРАВИЛО


async def _принятая(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    текст: str,
    *,
    author: int,
    chat: str,
    человеком: bool = False,
    **поля: Any,
) -> uuid.UUID:
    """Строка адреса из реплики живым путём (`apply_inbound_event`), затем
    принята: автоматикой (`resolved_by_id` пуст) или человеком. `поля` —
    правки строки «как было» после рождения."""
    await apply_inbound_event(db, redis, account, событие(текст, author=author, chat=chat))
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                sa.select(ClientAddressCandidate)
                .join(Message, Message.id == ClientAddressCandidate.message_id)
                .where(Message.body == текст)
            )
        ).scalar_one()
        row.status = CANDIDATE_ACCEPTED
        row.resolved_at = T0
        row.resolved_by_id = uuid.uuid4() if человеком else None
        for k, v in поля.items():
            setattr(row, k, v)
        await s.commit()
        return row.id


async def _golden(db_sessionmaker: Any, capsys: Any, **kw: Any) -> tuple[int, str]:
    """Прогон `--golden`; возвращает код выхода (0 — вернулась без Exit) и вывод."""
    from app.cli import run_address_reparse

    код = 0
    async with db_sessionmaker() as s:
        try:
            await run_address_reparse(s, days=30, dry_run=True, golden=True, **kw)
        except typer.Exit as e:
            код = e.exit_code
    return код, capsys.readouterr().out


async def _две_строки(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any
) -> tuple[uuid.UUID, uuid.UUID]:
    авто = await _принятая(
        db, redis, account, db_sessionmaker, "ул Ленина 5", author=5001, chat="chat-a"
    )
    руками = await _принятая(
        db, redis, account, db_sessionmaker, "ул Мира 7", author=5002, chat="chat-b", человеком=True
    )
    return авто, руками


async def test_golden_нули_при_неизменном_разборе_и_ничего_не_пишет(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any, engine: AsyncEngine
) -> None:
    """Разбор тот же — все ключи на месте и нули напечатаны (шаг выкатки читает
    «0/0» грепом, ключ не появляется «только при ≥ 1»); код выхода 0; ни одного
    UPDATE/INSERT/DELETE (сторож, который правку ловит наверняка, — ниже, на
    репликах C)."""
    await _две_строки(db, redis, account, db_sessionmaker)
    записи: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            записи.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        код, вывод = await _golden(db_sessionmaker, capsys)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert код == 0
    assert (
        "golden строк=2 авто=1 руками=1 ключ_изменился=0 строки_нет=0 уровень_упал=0 "
        "не_судимо=0 сухой_прогон=True"
    ) in вывод
    assert записи == []
    async with db_sessionmaker() as s:
        строки = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert [r.status for r in строки] == [CANDIDATE_ACCEPTED, CANDIDATE_ACCEPTED]


async def test_golden_ключ_изменился_код_1_и_пример_маской(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any, engine: AsyncEngine
) -> None:
    """Строка хранит другой ключ, чем даёт нынешний разбор её реплики —
    `ключ_изменился=1`, пример «было → стало» масками, код выхода 1; строка в
    базе не тронута и при расхождении."""
    авто, _ = await _две_строки(db, redis, account, db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, авто)
        row.street, row.value = "ул Пушкина", "ул Пушкина, 5"
        await s.commit()
    записи: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            записи.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        код, вывод = await _golden(db_sessionmaker, capsys)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert код == 1
    assert "golden строк=2 авто=1 руками=1 ключ_изменился=1 строки_нет=0 уровень_упал=0" in вывод
    assert "  ул Пушкина, 5 → ул Ленина, 5" in вывод
    assert записи == []
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, авто)
        assert (row.value, row.status) == ("ул Пушкина, 5", CANDIDATE_ACCEPTED)


async def test_golden_строки_нет_код_1(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """Реплика по нынешнему разбору без адреса («здравствуйте») — `строки_нет=1`,
    пример «было → —», код выхода 1."""
    авто, _ = await _две_строки(db, redis, account, db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, авто)
        msg = (await s.execute(sa.select(Message).where(Message.id == row.message_id))).scalar_one()
        msg.body = "здравствуйте"
        await s.commit()
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 1
    assert "ключ_изменился=0 строки_нет=1 уровень_упал=0" in вывод
    assert "  ул Ленина, 5 → —" in вывод


async def test_golden_ключ_сравнивается_семантически(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """Строка склеена под другим написанием («Ленина, 5» при реплике «ул Ленина
    5», как делает `clients._та_же_строка`): строковое `value` расходится, а
    адрес тот же — `ключ_изменился=0`, код 0. Дом без улицы («посёлок
    Сосново, 9»): ядра улицы нет, равные строки — тот же адрес."""
    авто, _ = await _две_строки(db, redis, account, db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, авто)
        row.street, row.value = "Ленина", "Ленина, 5"
        await s.commit()
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0 and "ключ_изменился=0 строки_нет=0" in вывод
    await _принятая(
        db, redis, account, db_sessionmaker, "Поселок Сосново дом 9", author=5003, chat="chat-c"
    )
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0 and "golden строк=3 авто=2 руками=1 ключ_изменился=0 строки_нет=0" in вывод


async def test_golden_уровень_упал_отдельно_от_ключа(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """«Ленина 5» без вопроса оператора — C; строка приняла её как A: уровень
    упал, ключ тот же — `уровень_упал=1`, код выхода 0 (в ворота не входит)."""
    await _принятая(
        db, redis, account, db_sessionmaker, "Ленина 5", author=5001, chat="chat-a", level="A"
    )
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0
    assert "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=1" in вывод
    assert "  Ленина, 5 → Ленина, 5 (уровень A→C)" in вывод


async def test_golden_ничего_не_пишет_когда_разбор_ходит_в_базу(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any, engine: AsyncEngine
) -> None:
    """Сторож записи там, где он что-то ловит: у реплики уровня C разбор идёт
    в базу за вопросом оператора, и правка ПРЕДЫДУЩЕЙ строки в сессии ушла бы
    UPDATE'ом автосбросом перед этим запросом — до всякого отката. У реплик
    уровня A (`ул …`) запросов нет, и там откат скрыл бы правку."""
    await _принятая(
        db, redis, account, db_sessionmaker, "Ленина 5", author=5001, chat="chat-a", level="A"
    )
    await _принятая(
        db, redis, account, db_sessionmaker, "Мира 7", author=5002, chat="chat-b", level="A"
    )
    записи: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            записи.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        код, вывод = await _golden(db_sessionmaker, capsys)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert код == 0 and "уровень_упал=2" in вывод
    assert записи == []


async def test_golden_выборка_только_принятые_дома_A_B(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """В корпус входят принятые дома A/B не от модели-читателя: уровень C,
    источник `llm` и нерешённая строка — мимо."""
    await _принятая(db, redis, account, db_sessionmaker, "ул Ленина 5", author=5001, chat="chat-a")
    await _принятая(
        db, redis, account, db_sessionmaker, "ул Мира 7", author=5002, chat="chat-b", level="C"
    )
    await _принятая(
        db,
        redis,
        account,
        db_sessionmaker,
        "ул Гагарина 9",
        author=5003,
        chat="chat-c",
        source=CANDIDATE_SOURCE_LLM,
    )
    await apply_inbound_event(
        db, redis, account, событие("ул Кирова 3", author=5004, chat="chat-d")
    )
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0
    assert "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=0" in вывод


async def test_golden_несовместим_с_боевым_прогоном_и_auto(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """`--golden --no-dry-run` и `--golden --auto` — код 2 до любого чтения;
    `--rules` без сухого прогона — тоже 2."""
    from app.cli import run_address_reparse

    await _две_строки(db, redis, account, db_sessionmaker)
    for kw in (
        {"dry_run": False, "golden": True},
        {"dry_run": True, "golden": True, "auto": True},
        {"dry_run": False, "rules_text": "x=on"},
    ):
        async with db_sessionmaker() as s:
            with pytest.raises(typer.Exit) as e:
                await run_address_reparse(s, days=30, **kw)
        assert e.value.exit_code == 2, kw
    вывод = capsys.readouterr().out
    # Три отказа — три строки причины, итога золотого корпуса нет.
    assert "golden строк=" not in вывод and вывод.count("\n") == 3
    async with db_sessionmaker() as s:
        строки = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert all(r.status == CANDIDATE_ACCEPTED for r in строки)


async def test_golden_rules_поверх_настройки_один_раз_на_прогон(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
    правило: str,
) -> None:
    """`--rules имя=on` ложится ПОВЕРХ настройки `address_parse.rules`: пара
    консоли побеждает, остальные перекрытия настройки остаются; настройка
    читается один раз на прогон; чужое имя — код 2."""
    await _две_строки(db, redis, account, db_sessionmaker)
    второе = "q_golden_other"
    monkeypatch.setitem(address_parse.PARSE_RULES, второе, address_parse.PARSE_OFF)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_PARSE_RULES: f"{правило}=shadow,{второе}=shadow"}, user_id=None
        )
        await s.commit()
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
    код, _ = await _golden(db_sessionmaker, capsys)
    assert код == 0 and len(разборы) == 2
    # Поверх ВСЕГО реестра (с пакета 7a в нём настоящие имена), не точный литерал.
    реестр = dict(address_parse.PARSE_RULES)
    assert all(r == {**реестр, правило: "shadow", второе: "shadow"} for r in разборы), разборы
    assert len(чтений) == 1
    разборы.clear()
    чтений.clear()
    код, _ = await _golden(db_sessionmaker, capsys, rules_text=f"{правило}=on")
    assert код == 0 and len(разборы) == 2
    assert all(r == {**реестр, правило: "on", второе: "shadow"} for r in разборы), разборы
    assert len(чтений) == 1
    код, вывод = await _golden(db_sessionmaker, capsys, rules_text="nope=on")
    assert код == 2 and "неизвестное правило разбора «nope»" in вывод


async def test_golden_порции_откатом(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Строки грузятся порциями заново и каждая порция откатывается: откат
    протухает объекты сессии, и обход по загруженным один раз строкам падал бы
    MissingGreenlet на второй порции (образец `run_backfill_cards`)."""
    from app import cli

    await _две_строки(db, redis, account, db_sessionmaker)
    await _принятая(
        db, redis, account, db_sessionmaker, "ул Гагарина 9", author=5003, chat="chat-c"
    )
    monkeypatch.setattr(cli, "_ПОРЦИЯ_GOLDEN", 1)
    откатов: list[int] = []
    from app.cli import run_address_reparse

    async with db_sessionmaker() as s:
        исходный = s.rollback

        async def rollback() -> None:
            откатов.append(1)
            await исходный()

        monkeypatch.setattr(s, "rollback", rollback)
        await run_address_reparse(s, days=30, dry_run=True, golden=True)
    assert len(откатов) == 3
    assert "golden строк=3 авто=2 руками=1 ключ_изменился=0 строки_нет=0" in capsys.readouterr().out


async def test_обёртка_address_reparse_несёт_golden_и_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--golden` и `--rules` доходят от консоли до `run_address_reparse`; без
    них вызов идёт старой сигнатурой (подмены соседних сторожей её и ждут)."""
    import typer.main

    from app import cli

    команда = typer.main.get_command(cli.app).commands["address-reparse"]  # type: ignore[attr-defined]
    параметры = {p.name: p for p in команда.params}
    assert параметры["golden"].default is False and "--golden" in параметры["golden"].opts
    assert параметры["rules"].default is None and "--rules" in параметры["rules"].opts
    перехват: list[Any] = []
    вызовы: list[dict[str, Any]] = []

    async def _fake(db: Any, **kwargs: Any) -> None:
        вызовы.append(kwargs)

    monkeypatch.setattr(cli, "run_address_reparse", _fake)
    monkeypatch.setattr(cli, "_run", перехват.append)
    cli.address_reparse(days=30, dry_run=True, auto=False, golden=True, rules="x=on")
    cli.address_reparse(days=7, dry_run=True, auto=False, golden=False, rules=None)
    for main in перехват:
        await main(None)
    assert вызовы == [
        {"days": 30, "dry_run": True, "auto": False, "golden": True, "rules_text": "x=on"},
        {"days": 7, "dry_run": True, "auto": False},
    ]


# ── ревью 21.09, #13: строки, чью реплику разбор не видел никогда ────────────


ГЕОТОЧКА = {
    "media_id": "avito_location_1",
    "kind": "file",
    "name": "Геопозиция",
    "size": None,
    "avito_type": "location",
    "lat": 56.0123,
    "lon": 37.1234,
    "address": "Московская область, городской округ Химки, посёлок Берёзово, "
    "садоводческое некоммерческое товарищество Берёзово, 27",
}


async def _принять_все(db_sessionmaker: Any) -> list[ClientAddressCandidate]:
    """Принять автоматикой все строки стенда (в бою — автозапись); вернуть их."""
    async with db_sessionmaker() as s:
        строки = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
        for row in строки:
            row.status = CANDIDATE_ACCEPTED
            row.resolved_at = T0
            row.resolved_by_id = None
        await s.commit()
        return строки


async def test_golden_геоточка_авито_не_судится(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """Геоточка Авито (вложение `location` без тела) рождает строку house/A, и
    `разбор_реплики_сейчас` по её реплике молчит по построению — речи нет.
    Это не «строки_нет» (разбор её не видел никогда), а `не_судимо`: ключ
    печатается с нулём всегда, в ворота не входит — код выхода 0, примера
    «→ —» нет. Единственный предикат — `inbound.реплика_без_адреса`, тот же,
    что у догона `--auto` и сторожа автозаписи."""
    from dataclasses import replace

    точка = replace(событие("", author=5001, chat="chat-geo"), text=None, attachments=[ГЕОТОЧКА])
    await apply_inbound_event(db, redis, account, точка)
    строки = await _принять_все(db_sessionmaker)
    assert [(r.kind, r.level, r.house, r.geo_provider) for r in строки] == [
        ("house", address_parse.LEVEL_A, "27", "avito")
    ]
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0
    assert (
        "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=0 "
        "не_судимо=1 сухой_прогон=True"
    ) in вывод
    assert "→ —" not in вывод


async def test_golden_дом_к_месту_не_судится(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """«п. Сосновка» → «дом 9» отдельной репликой: строка house/A «посёлок
    Сосновка, 9» ссылается на реплику «дом 9», в которой только дом — разбор
    одной реплики его не читает (`_дом_к_месту` живёт в приёме). Обе строки
    приняты; в корпус входит только дом — `строк=1`, `строки_нет=0`,
    `не_судимо=1`, код 0."""
    await apply_inbound_event(
        db, redis, account, событие("п. Сосновка", author=5001, chat="chat-s")
    )
    await apply_inbound_event(db, redis, account, событие("дом 9", author=5001, chat="chat-s"))
    строки = await _принять_все(db_sessionmaker)
    assert sorted((r.kind, r.level, r.value) for r in строки) == [
        ("house", address_parse.LEVEL_A, "посёлок Сосновка, 9"),
        ("place", address_parse.LEVEL_A, "посёлок Сосновка"),
    ]
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 0
    assert (
        "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=0 не_судимо=1"
        in вывод
    )
    assert "→ —" not in вывод


# ── ревью 21.09, #14: фатальные примеры печатаются первыми ───────────────────

#: Реплики без «ул» — нынешний разбор даёт C; строка приняла их как A.
_РЕПЛИКИ_C = (
    "Ленина 5",
    "Гоголя 3",
    "Мира 7",
    "Кирова 3",
    "Садовая 2",
    "Лесная 4",
    "Полевая 6",
    "Школьная 8",
    "Зелёная 1",
    "Центральная 9",
)


async def test_golden_примеры_смены_ключа_не_вытесняются_уровнем(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """Десять законных падений уровня раньше по `detected_at` и одна смена
    ключа позже: ворота падают из-за ключа, и его пример обязан быть напечатан
    — фатальные (смена ключа, строки нет) идут первыми, уровень — в остаток
    до общего потолка 10."""
    from datetime import timedelta

    for i, текст in enumerate(_РЕПЛИКИ_C):
        await _принятая(
            db,
            redis,
            account,
            db_sessionmaker,
            текст,
            author=5100 + i,
            chat=f"chat-l{i}",
            level="A",
            detected_at=T0 + timedelta(minutes=i),
        )
    поздняя = await _принятая(
        db,
        redis,
        account,
        db_sessionmaker,
        "ул Гоголя 3",
        author=5199,
        chat="chat-k",
        detected_at=T0 + timedelta(hours=1),
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, поздняя)
        row.street, row.value = "ул Пушкина", "ул Пушкина, 3"
        await s.commit()
    код, вывод = await _golden(db_sessionmaker, capsys)
    assert код == 1
    assert "golden строк=11 авто=11 руками=0 ключ_изменился=1 строки_нет=0 уровень_упал=10" in вывод
    примеры = [строка for строка in вывод.splitlines() if строка.startswith("  ")]
    assert примеры[0] == "  ул Пушкина, 3 → ул Гоголя, 3"
    assert len(примеры) == 10 and all("(уровень A→C)" in п for п in примеры[1:])


# ── пакет 7a §B.2: `--scope all` ─────────────────────────────────────────────


async def _стенд_scope(db: Any, redis: Any, account: Any, db_sessionmaker: Any) -> uuid.UUID:
    """Пять строк-домов: принятая A (ворота), нерешённая C и отклонённая B с
    репликами, где нынешний разбор адреса не видит (снято), нерешённая A с
    чужим ключом, принятая C без адреса (вне ворот, хоть и принятая)."""
    from app.models.client import CANDIDATE_PENDING, CANDIDATE_REJECTED

    ворота = await _принятая(
        db, redis, account, db_sessionmaker, "ул Ленина 5", author=5001, chat="c-a"
    )
    строки = {
        "ул Мира 7": ("c-b", 5002, {"status": CANDIDATE_PENDING, "level": "C"}, "здравствуйте"),
        "ул Гагарина 9": ("c-c", 5003, {"status": CANDIDATE_REJECTED, "level": "B"}, "добрый день"),
        "ул Кирова 3": ("c-d", 5004, {"status": CANDIDATE_PENDING}, None),
        "ул Садовая 2": ("c-e", 5005, {"level": "C"}, "здравствуйте"),
    }
    for текст, (chat, author, поля, новое_тело) in строки.items():
        row_id = await _принятая(
            db, redis, account, db_sessionmaker, текст, author=author, chat=chat, **поля
        )
        async with db_sessionmaker() as s:
            row = await s.get(ClientAddressCandidate, row_id)
            if новое_тело is None:
                row.street, row.value = "ул Пушкина", "ул Пушкина, 3"
            else:
                msg = (
                    await s.execute(sa.select(Message).where(Message.id == row.message_id))
                ).scalar_one()
                msg.body = новое_тело
            await s.commit()
    return ворота


async def test_golden_scope_all_считает_снятые_по_статусам(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any, engine: AsyncEngine
) -> None:
    """`--scope all`: первая строка — те же принятые A/B слово в слово (код 0,
    хотя вне ворот снято три и один ключ сменился), затем `scope=all строк=5`,
    группы по статусу/уровню (нули `снято`/`ключ_изменился` не печатаются) и
    снятые вне ворот масками; принятая C — вне ворот. Ничего не пишет. Смена
    ключа у принятой A после этого — код 1 по-прежнему только из-за неё."""
    ворота = await _стенд_scope(db, redis, account, db_sessionmaker)
    записи: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            записи.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        код, вывод = await _golden(db_sessionmaker, capsys, scope="all")
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert код == 0 and записи == []
    строки = вывод.splitlines()
    assert строки[0] == (
        "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=0 "
        "не_судимо=0 сухой_прогон=True"
    )
    assert строки[1] == "golden scope=all строк=5"
    assert строки[2:7] == [
        "  строк[accepted/A]=1",
        "  строк[accepted/C]=1 снято[accepted/C]=1",
        "  строк[pending/A]=1 ключ_изменился[pending/A]=1",
        "  строк[pending/C]=1 снято[pending/C]=1",
        "  строк[rejected/B]=1 снято[rejected/B]=1",
    ]
    assert sorted(строки[7:]) == [
        "  снято[accepted/C] ул Садовая, 2",
        "  снято[pending/C] ул Мира, 7",
        "  снято[rejected/B] ул Гагарина, 9",
    ]
    assert "→ —" not in вывод
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, ворота)
        row.street, row.value = "ул Пушкина", "ул Пушкина, 5"
        await s.commit()
    код, вывод = await _golden(db_sessionmaker, capsys, scope="all")
    assert код == 1
    assert вывод.splitlines()[:3] == [
        "golden строк=1 авто=1 руками=0 ключ_изменился=1 строки_нет=0 уровень_упал=0 "
        "не_судимо=0 сухой_прогон=True",
        "  ул Пушкина, 5 → ул Ленина, 5",
        "golden scope=all строк=5",
    ]
    assert "  строк[accepted/A]=1 ключ_изменился[accepted/A]=1" in вывод


async def test_golden_scope_accepted_печать_как_сегодня(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any
) -> None:
    """Умолчание и явный `scope="accepted"` — печать прежняя, без групп и без
    `scope=all`; строки вне ворот в счёт не входят."""
    await _стенд_scope(db, redis, account, db_sessionmaker)
    код, по_умолчанию = await _golden(db_sessionmaker, capsys)
    assert код == 0
    код, явно = await _golden(db_sessionmaker, capsys, scope="accepted")
    assert код == 0 and явно == по_умолчанию
    assert по_умолчанию == (
        "golden строк=1 авто=1 руками=0 ключ_изменился=0 строки_нет=0 уровень_упал=0 "
        "не_судимо=0 сухой_прогон=True\n"
    )


async def test_golden_scope_мусор_и_без_golden_код_2(
    db: Any, redis: Any, account: Any, db_sessionmaker: Any, capsys: Any, engine: AsyncEngine
) -> None:
    """`--scope мусор` — код 2 с перечнем допустимых до чтения базы (ни одного
    SELECT); `--scope all` без `--golden` — тоже 2: опция, которая молча не
    действует, — ловушка."""
    from app.cli import run_address_reparse

    await _две_строки(db, redis, account, db_sessionmaker)
    запросы: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        запросы.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        for kw in (
            {"golden": True, "scope": "мусор"},
            {"golden": True, "scope": "ALL"},
            {"golden": False, "scope": "all"},
        ):
            async with db_sessionmaker() as s:
                with pytest.raises(typer.Exit) as e:
                    await run_address_reparse(s, days=30, dry_run=True, **kw)
            assert e.value.exit_code == 2, kw
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert запросы == []
    вывод = capsys.readouterr().out
    assert вывод.count("\n") == 3 and "golden строк=" not in вывод
    assert "неизвестная выборка «мусор»; допустимы: accepted, all" in вывод
    assert "--scope действует только с --golden" in вывод


async def test_обёртка_address_reparse_несёт_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--scope` доходит от консоли до `run_address_reparse` только когда он не
    умолчание: прежние вызовы (подмены соседних сторожей со старой сигнатурой)
    идут без него, мусор доезжает до проверки в `run_address_reparse`."""
    import typer.main

    from app import cli

    команда = typer.main.get_command(cli.app).commands["address-reparse"]  # type: ignore[attr-defined]
    параметры = {p.name: p for p in команда.params}
    assert параметры["scope"].default == "accepted" and "--scope" in параметры["scope"].opts
    перехват: list[Any] = []
    вызовы: list[dict[str, Any]] = []

    async def _fake(db: Any, **kwargs: Any) -> None:
        вызовы.append(kwargs)

    monkeypatch.setattr(cli, "run_address_reparse", _fake)
    monkeypatch.setattr(cli, "_run", перехват.append)
    cli.address_reparse(days=30, dry_run=True, auto=False, golden=True, rules=None, scope="all")
    cli.address_reparse(
        days=30, dry_run=True, auto=False, golden=True, rules=None, scope="accepted"
    )
    cli.address_reparse(days=30, dry_run=True, auto=False, golden=True, rules=None, scope="мусор")
    cli.address_reparse(days=7, dry_run=True, auto=False, golden=False, rules=None)
    for main in перехват:
        await main(None)
    assert вызовы == [
        {"days": 30, "dry_run": True, "auto": False, "golden": True, "scope": "all"},
        {"days": 30, "dry_run": True, "auto": False, "golden": True},
        {"days": 30, "dry_run": True, "auto": False, "golden": True, "scope": "мусор"},
        {"days": 7, "dry_run": True, "auto": False},
    ]
