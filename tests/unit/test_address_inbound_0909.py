"""Адрес из переписки ложится РЯДОМ с карточкой, а не в неё.

⚠ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТ ЭТОТ ФАЙЛ, — ОТСУТСТВИЕ ЗАПИСИ. У телефона автозапись в
пустую карточку возможна и оправдана: ошибка стоит звонка постороннему, и её
слышно сразу. У адреса ошибка — мастер, уехавший не туда: потерянный день
бригады и клиент, прождавший впустую. Опереться на проверку «существует ли такой
дом» нельзя, справочника у нас нет. Значит `clients.address` не заполняет никто,
кроме человека, и это правило обязано пережить любую будущую правку.

Вторая половина — СКЛЕЙКА ИЗ НЕСКОЛЬКИХ СООБЩЕНИЙ. Замер боя: в 23,5 % адресных
диалогов адрес размазан по репликам («Ленина 5» → «кв 3, второй подъезд»). Ради
этого ключ уникальности взят без квартиры, и части дописываются в ту же строку.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import Client, ClientAddressCandidate
from app.services import app_settings
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

AVITO_USER_ID = 111222444
T0 = datetime(2026, 9, 9, 10, 0, 0, tzinfo=UTC)


def событие(текст: str, *, msg: str = "am-1", when: datetime = T0) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-addr",
        external_message_id=msg,
        author_id=999002,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Пётр Адресов",
        item_title="Ремонт телевизора",
        item_url="https://avito.ru/item/2",
        item_price="от 1500 ₽",
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


async def строки(db_sessionmaker) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as s:
        return list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())


async def test_адрес_ложится_кандидатом_а_карточка_остаётся_пустой(
    db, redis, account, db_sessionmaker
):
    """⚠ ДИВЕРСИЯ: заставить `_maybe_extract_address` писать в `client.address` —
    краснеет вторая половина проверки, и это та самая беда."""
    await apply_inbound_event(db, redis, account, событие("Приезжайте на ул. Ленина 5"))

    строчки = await строки(db_sessionmaker)
    assert len(строчки) == 1
    assert строчки[0].value == "ул. Ленина, 5"
    assert строчки[0].status == "pending"
    assert строчки[0].level == "A"
    # Цитата — кусок сообщения ровно как написан: оператор сверяет догадку с ним.
    assert строчки[0].raw == "ул. Ленина 5"

    async with db_sessionmaker() as s:
        client = (await s.execute(sa.select(Client))).scalars().one()
    assert client.address is None, "распознанный адрес попал в карточку молча"
    assert client.address_set_by_id is None


async def test_части_дописываются_следующим_сообщением(db, redis, account, db_sessionmaker):
    """«Ленина 5» → «кв 3, 2 подъезд»: одна строка, а не три вопроса оператору.

    ⚠ ДИВЕРСИЯ: убрать вызов `_дописать_части` — квартира и подъезд не
    появляются, и оператор видит адрес без входа в квартиру.
    """
    await apply_inbound_event(db, redis, account, событие("Мой адрес ул. Ленина 5"))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("кв 3, 2 подъезд", msg="am-2", when=T0.replace(minute=1)),
    )

    строчки = await строки(db_sessionmaker)
    assert len(строчки) == 1, "части завели вторую строку вместо дописывания"
    assert строчки[0].office == "3"
    assert строчки[0].entrance == "2"
    # Цитата осталась от сообщения, где названы улица и дом: «кв 3, 2 подъезд»
    # доказательством адреса быть не может — в нём нет ни улицы, ни дома.
    assert строчки[0].raw == "ул. Ленина 5"


async def test_позднее_значение_части_побеждает_раннее(db, redis, account, db_sessionmaker):
    """Человек, повторяющий квартиру, почти всегда себя поправляет."""
    await apply_inbound_event(db, redis, account, событие("ул. Ленина 5 кв 3"))
    await apply_inbound_event(
        db, redis, account, событие("извините, кв 7", msg="am-2", when=T0.replace(minute=1))
    )

    строчки = await строки(db_sessionmaker)
    assert len(строчки) == 1
    assert строчки[0].office == "7"


async def test_пустая_часть_не_затирает_известную(db, redis, account, db_sessionmaker):
    """«Ленина 5» без квартиры не обязано стирать квартиру, названную раньше."""
    await apply_inbound_event(db, redis, account, событие("ул. Ленина 5 кв 3"))
    await apply_inbound_event(
        db, redis, account, событие("ул. Ленина 5", msg="am-2", when=T0.replace(minute=1))
    )

    строчки = await строки(db_sessionmaker)
    assert len(строчки) == 1
    assert строчки[0].office == "3"


async def test_выключатель_останавливает_разбор(db, redis, account, db_sessionmaker):
    """Разбор трогает ЧУЖИЕ данные, и остановить его владелец обязан щелчком."""
    await app_settings.set_many(db, {app_settings.ADDRESS_DETECT_ENABLED: False}, user_id=None)
    await db.commit()

    await apply_inbound_event(db, redis, account, событие("Приезжайте на ул. Ленина 5"))
    assert await строки(db_sessionmaker) == []


async def test_не_адрес_строки_не_заводит(db, redis, account, db_sessionmaker):
    """Отрицательная проверка с живым остатком: сообщение обработано, строки нет.

    ⚠ БЕЗ ЭТОГО УСЛОВИЯ ПРОВЕРКА ЗЕЛЕНЕЛА БЫ И ОТ ТОГО, ЧТО ПРИЁМ ВООБЩЕ УПАЛ.
    Поэтому рядом проверяется, что сообщение доехало до базы.
    """
    from app.models import Message

    await apply_inbound_event(
        db, redis, account, событие("Телевизор Самсунг 55, гарантия 12 месяцев")
    )
    assert await строки(db_sessionmaker) == []
    async with db_sessionmaker() as s:
        сообщений = (await s.execute(sa.select(sa.func.count()).select_from(Message))).scalar_one()
    assert сообщений == 1, "приём не сработал — проверка мерила бы не то"


# --- проверка по карте ставится после commit'а (11.09) -----------------------


async def test_проверка_по_карте_ставится_на_новую_строку(db, redis, account, db_sessionmaker):
    """Задача бежит к строке по идентификатору — значит ставится ПОСЛЕ commit'а.

    ⚠ ДИВЕРСИЯ: перенести `enqueue_geocode` внутрь `_maybe_extract_address`
    (внутрь транзакции) — задача встаёт, но строки в базе ещё нет; здесь это
    видно так: ключ задачи есть, а строка в базе — по другому id или её нет.
    """
    await apply_inbound_event(db, redis, account, событие("Приезжайте на ул. Ленина 5"))
    (строчка,) = await строки(db_sessionmaker)
    assert строчка.geo_status == "pending"
    assert await redis.exists(f"arq:job:geocode:{строчка.id}")


async def test_догрузка_истории_к_карте_не_ходит(db, redis, account, db_sessionmaker):
    """Тысячи старых реплик разом — залп в чужую карту. Их возьмёт починка."""
    await apply_inbound_event(
        db, redis, account, событие("Приезжайте на ул. Ленина 5"), backfill=True
    )
    (строчка,) = await строки(db_sessionmaker)
    assert not await redis.exists(f"arq:job:geocode:{строчка.id}")


async def test_посёлок_названный_позже_дописывается_и_переспрашивает_карту(
    db, redis, account, db_sessionmaker
):
    """«Ленина 5» → «это посёлок Ударник, ул Ленина 5»: тот же ключ, новая проверка."""
    await apply_inbound_event(db, redis, account, событие("ул Ленина 5"))
    (строчка,) = await строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, строчка.id)
        row.geo_status = "exact"
        row.geo_formatted = "улица Ленина, 5, Орск"
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("это посёлок Ударник, ул Ленина 5", msg="am-2")
    )
    (после,) = await строки(db_sessionmaker)
    assert после.id == строчка.id
    assert (после.settlement, после.settlement_type) == ("Ударник", "посёлок")
    assert после.geo_status == "pending"


async def test_постановка_проверки_идёт_после_commit(
    db, redis, account, db_sessionmaker, monkeypatch
):
    """⚠ ДИВЕРСИЯ: перенести `enqueue_geocode` внутрь `_maybe_extract_address` —
    краснеет. Стережётся состояние сессии в момент постановки, а не ключ
    задачи: ключ есть и до, и после commit'а (ревью 11.09)."""
    from app.services import inbound as inbound_mod

    состояния: list[bool] = []

    async def подсмотреть(redis_, cid, **kw):  # noqa: ANN001
        состояния.append(db.in_transaction())
        return True

    monkeypatch.setattr(inbound_mod, "enqueue_geocode", подсмотреть)
    await apply_inbound_event(db, redis, account, событие("Приезжайте на ул. Ленина 5"))
    assert состояния == [False], f"постановка внутри транзакции: {состояния}"


async def test_карта_выключена_задача_не_ставится(db, redis, account, db_sessionmaker):
    """Строка ждёт `pending` и не крутит пустые задачи; включат — доберёт починка."""
    await app_settings.set_many(db, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
    await db.commit()
    await apply_inbound_event(db, redis, account, событие("Приезжайте на ул. Ленина 5"))
    (строчка,) = await строки(db_sessionmaker)
    assert строчка.geo_status == "pending"
    assert not await redis.exists(f"arq:job:geocode:{строчка.id}")


async def test_повтор_адреса_после_автозаписи_дописывает_квартиру(
    db, redis, account, db_sessionmaker
):
    """«ул Ленина 5 кв 7» после автозаписи обязано дописать квартиру в строку-источник.

    Ранний выход «этот адрес уже в карточке» стоял ДО записи и терял её, хотя
    голое «кв 7» без улицы дописывалось (ревью 11.09).
    """
    await apply_inbound_event(db, redis, account, событие("ул Ленина 5"))
    (строчка,) = await строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        card = await s.get(Client, строчка.client_id)
        card.address = "улица Ленина, 5, Орск"
        card.address_candidate_id = строчка.id
        card.address_value = строчка.value
        await s.commit()
    await apply_inbound_event(db, redis, account, событие("ул Ленина 5 кв 7", msg="am-2"))
    (после,) = await строки(db_sessionmaker)
    assert после.id == строчка.id
    assert после.office == "7"


async def test_строка_источник_ушла_каскадом_адрес_не_предлагается_заново(
    db, redis, account, db_sessionmaker
):
    """Отключение канала удаляет диалоги, строка-источник уходит каскадом, связь
    обнуляется — а «этот адрес уже в карточке» обязано узнаваться по ключу
    `address_value` (ревью 11.09)."""
    await apply_inbound_event(db, redis, account, событие("ул Ленина 5"))
    (строчка,) = await строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        card = await s.get(Client, строчка.client_id)
        card.address = "улица Ленина, 5, Орск"
        card.address_candidate_id = None  # так остаётся после каскада
        card.address_value = строчка.value
        row = await s.get(ClientAddressCandidate, строчка.id)
        await s.delete(row)
        await s.commit()
    await apply_inbound_event(db, redis, account, событие("ул Ленина 5", msg="am-3"))
    assert await строки(db_sessionmaker) == []
