"""Объявление у диалога: боевой Авито его в вебхуке не присылает.

КРИТИЧНЫЙ ДЕФЕКТ, РАДИ КОТОРОГО ЭТОТ ФАЙЛ (docs/33 §14а). Вебхук Мессенджера
несёт только числовой ``item_id`` — слова ``item_id`` во всём серверном коде не
было ни разу, разбор ищет развёрнутый объект. Диалог создавался с пустым
объявлением, а дописать его было НЕЧЕМ: во всём коде четыре присваивания
``item_title`` и ни одного обновления. Сверка каждый час тянула чаты, честно
доставала из них название — и выбрасывала, потому что строка диалога уже есть.
Клиенту это уезжало как «по объявлению «»» из шаблона бота.

Первый тест — ровно та дыра, через которую дефект прошёл: разбора вебхука БЕЗ
ключа ``item`` не проверял никто.

Проверка ломанием: верните в inbound условие постановки задачи к одному
``client_created_without_name`` — падает ``test_conversation_without_item_asks``;
уберите дописывание в сверке — падает ``test_reconciliation_fills_empty_item``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import AvitoAdapter, InboundEvent, _extract_item
from app.models import Client, Conversation

# --- 1. разбор: как выглядит боевой вебхук ------------------------------------


def test_webhook_without_item_key_yields_nothing() -> None:
    """Боевой вебхук: только item_id. Разбор обязан вернуть пустоту, а не упасть."""
    title, url, price = _extract_item({"id": "msg-1", "chat_id": "c-1", "item_id": 4242424242})
    assert (title, url, price) == (None, None, None)


def test_webhook_with_expanded_item_still_works() -> None:
    """Расширение имитатора: развёрнутый объект по-прежнему разбирается."""
    title, url, price = _extract_item(
        {"item": {"title": "Ремонт холодильников", "url": "https://avito.ru/1", "price": 3500}}
    )
    assert title == "Ремонт холодильников"
    assert url == "https://avito.ru/1"
    assert price == "3500"


def test_chat_card_carries_the_item() -> None:
    """Карточка чата объявление НЕСЁТ — на ней и держится вся починка."""
    info = AvitoAdapter.parse_chat(
        {
            "id": "c-1",
            "users": [{"id": 111222333, "name": "Мы"}, {"id": 999, "name": "Клиент"}],
            "context": {
                "type": "item",
                "value": {
                    "title": "Стиральная машина, ремонт",
                    "url": "https://avito.ru/item/7",
                    "price_string": "от 1 500 ₽",
                },
            },
        },
        account_user_id=111222333,
    )
    assert info.item_title == "Стиральная машина, ремонт"
    assert info.item_url == "https://avito.ru/item/7"
    assert info.item_price == "от 1 500 ₽"


# --- 2. диалог без объявления просит дозагрузку -------------------------------


async def test_conversation_without_item_asks_for_enrichment(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Диалог создан без объявления — задача дозагрузки поставлена.

    Раньше задача ставилась ТОЛЬКО для нового клиента без имени. Диалог у уже
    известного клиента (второе обращение — обычное дело у Авито: тот же человек
    пишет по другому объявлению) оставался с прочерком навсегда.
    """
    from app.services import inbound as inbound_svc

    asked: list[uuid.UUID] = []

    async def _spy(_redis: Any, conversation_id: uuid.UUID) -> None:
        asked.append(conversation_id)

    monkeypatch.setattr(inbound_svc, "enqueue_enrich_client", _spy)

    account = await make_avito_account()
    event = InboundEvent(
        external_chat_id="chat-no-item",
        external_message_id="m-1",
        author_id=999777,
        account_user_id=account.avito_user_id,
        text="Здравствуйте",
        created_at=datetime.now(UTC),
        client_name="Пётр",  # имя есть — прежнего повода для задачи нет
        item_title=None,  # объявления нет — это и есть боевой случай
        item_url=None,
        item_price=None,
    )
    async with db_sessionmaker() as db:
        await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)

    assert len(asked) == 1, "диалог без объявления обязан просить дозагрузку"


async def test_conversation_with_item_does_not_ask(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Объявление пришло и про профиль уже спрашивали — лишнего похода нет.

    ⚠ ВТОРОЕ УСЛОВИЕ ДОБАВЛЕНО 02.09 И МЕНЯЕТ СМЫСЛ ТЕСТА ЧЕСТНО. С этого дня
    у постановки задачи три повода, а не два: третий — «про ссылку на профиль
    клиента ещё не спрашивали» (просьба владельца). Он гаснет навсегда после
    первого же вопроса к Авито, успешного или пустого, поэтому цена конечна:
    один вопрос на клиента, а не на сообщение.

    Проверяемое правило при этом осталось прежним — «лишнего похода в чужой API
    не делаем», — просто «лишний» теперь значит «всё известно, включая профиль».
    Разница видна в соседнем тесте: он требует, чтобы у клиента БЕЗ отметки
    задача встала даже при известных имени и объявлении.
    """
    from app.services import inbound as inbound_svc

    asked: list[uuid.UUID] = []

    async def _spy(_redis: Any, conversation_id: uuid.UUID) -> None:
        asked.append(conversation_id)

    monkeypatch.setattr(inbound_svc, "enqueue_enrich_client", _spy)

    account = await make_avito_account(avito_user_id=222333444)
    event = InboundEvent(
        external_chat_id="chat-with-item",
        external_message_id="m-2",
        author_id=999778,
        account_user_id=account.avito_user_id,
        text="Здравствуйте",
        created_at=datetime.now(UTC),
        client_name="Анна",
        item_title="Ремонт посудомоечных машин",
        item_url="https://avito.ru/item/9",
        item_price="2 000 ₽",
    )
    async with db_sessionmaker() as db:
        await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)
        # Первое сообщение всё-таки спросит — про профиль. Гасим повод так же,
        # как это сделало бы само обогащение, и повторяем.
        client_row = (
            await db.execute(sa.select(Client).where(Client.external_id == "999778"))
        ).scalar_one()
        client_row.profile_checked_at = datetime(2026, 9, 2, tzinfo=UTC)
        await db.commit()
    asked.clear()

    async with db_sessionmaker() as db:
        event2 = replace(event, external_message_id="m-2b")
        await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event2)

    assert asked == []


async def test_known_client_still_asks_until_profile_was_checked(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """⚠ БЕЗ ЭТОГО ПОВОДА ССЫЛКА НЕ ПОЯВИТСЯ НИ У ОДНОГО ДАВНЕГО КЛИЕНТА.

    Обогащение умеет добирать профиль (просьба владельца 02.09), но задача
    ставилась по двум поводам: нет имени, нет объявления. У клиента, с которым
    переписываются давно, известно и то и другое — значит задача не встала бы
    ни разу, и кнопка не появилась бы. «Написано, но не подключено» в чистом
    виде; этот проект на таком попадался много раз.
    """
    from app.services import inbound as inbound_svc

    asked: list[uuid.UUID] = []

    async def _spy(_redis: Any, conversation_id: uuid.UUID) -> None:
        asked.append(conversation_id)

    monkeypatch.setattr(inbound_svc, "enqueue_enrich_client", _spy)

    account = await make_avito_account(avito_user_id=222333445)
    event = InboundEvent(
        external_chat_id="chat-known-client",
        external_message_id="m-3",
        author_id=999779,
        account_user_id=account.avito_user_id,
        text="Здравствуйте",
        created_at=datetime.now(UTC),
        client_name="Анна",  # имя есть
        item_title="Ремонт посудомоечных машин",  # объявление есть
        item_url="https://avito.ru/item/9",
        item_price="2 000 ₽",
    )
    async with db_sessionmaker() as db:
        await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)

    assert len(asked) == 1, "про профиль ещё не спрашивали — задача обязана встать"


class _FakeRedis:
    async def publish(self, *_a: Any, **_kw: Any) -> int:
        return 0

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        return True

    async def get(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def delete(self, *_a: Any) -> int:
        return 1

    async def incr(self, *_a: Any, **_kw: Any) -> int:
        return 1

    async def expire(self, *_a: Any, **_kw: Any) -> bool:
        return True


# --- 3. задача дописывает объявление, но не затирает известное ----------------


async def test_enrichment_fills_empty_item(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    from app.integrations.avito.adapter import ChatInfo
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=333444555)
    async with db_sessionmaker() as db:
        from app.models import Client

        client_row = Client(id=uuid.uuid4(), channel="avito", external_id="900", name="Иван")
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-fill",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
        )
        db.add(conv)
        await db.commit()
        conv_id = conv.id

    async def _fake_fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        return ChatInfo(
            external_chat_id="chat-fill",
            client_external_id="900",
            client_name="Иван",
            item_title="Ремонт духовых шкафов",
            item_url="https://avito.ru/item/11",
            item_price="3 000 ₽",
            unread_count=0,
            has_unread=False,
            last_message_at=None,
        )

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    ctx = {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}
    await client_enrich.enrich_client(ctx, conv_id)

    async with db_sessionmaker() as db:
        fresh = await db.get(Conversation, conv_id)
        assert fresh is not None
        assert fresh.item_title == "Ремонт духовых шкафов"
        assert fresh.item_url == "https://avito.ru/item/11"
        assert fresh.item_price == "3 000 ₽"


async def test_enrichment_does_not_overwrite_known_item(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Известное объявление задача не трогает — иначе прогон перестал бы быть
    безопасным к повтору.

    ⚠ 15.08 «всё уже известно» стало включать и ФОТО: обогащение добирает его
    тем же походом, что имя. Клиенту этого случая аватар выдан заранее — без
    него поход в Авито теперь ЗАКОНЕН (фото пусто, его надо добрать), и случай
    проверял бы не то, что задумывал. Обратная сторона — «фото пусто → поход
    есть» — заперта соседним test_enrichment_brings_the_client_photo.
    """
    from app.integrations.avito.adapter import ChatInfo
    from app.models import Client
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=444555666)
    async with db_sessionmaker() as db:
        client_row = Client(
            id=uuid.uuid4(),
            channel="avito",
            external_id="901",
            name="Ольга",
            avatar_url="https://static.avito.ru/i/olga.png",
            # Профиль отмечен спрошенным: с 02.09 обогащение ходит в Авито и
            # за ссылкой на профиль, и клиент, которого ещё не спрашивали,
            # делал бы поход законным. Здесь проверяется обратное — что при
            # известных полях похода нет вовсе.
            profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-known",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
            item_title="Уже знаем",
        )
        db.add(conv)
        await db.commit()
        conv_id = conv.id

    async def _fake_fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        raise AssertionError("за карточкой ходить незачем: всё уже известно")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    ctx = {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}
    await client_enrich.enrich_client(ctx, conv_id)

    async with db_sessionmaker() as db:
        fresh = await db.get(Conversation, conv_id)
        assert fresh is not None
        assert fresh.item_title == "Уже знаем"


async def test_enrichment_brings_the_client_photo(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Фото клиента приезжает тем же походом, что имя и объявление.

    Просьба владельца со снимками Jivo (14–15 августа): рядом с именем — фото,
    «а не просто Клиент». Авито отдаёт его в карточке чата; отдельного запроса
    под фото нет, и добавка обязана ехать в уже существующем обогащении.
    """
    from app.integrations.avito.adapter import ChatInfo
    from app.models import Client
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=333444777)
    async with db_sessionmaker() as db:
        client_row = Client(id=uuid.uuid4(), channel="avito", external_id="901", name="Инна")
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-ava",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
        )
        db.add(conv)
        await db.commit()
        conv_id, client_id = conv.id, client_row.id

    async def _fake_fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        return ChatInfo(
            external_chat_id="chat-ava",
            client_external_id="901",
            client_name="Инна",
            item_title="Ремонт телевизоров",
            item_url=None,
            item_price=None,
            unread_count=0,
            has_unread=False,
            last_message_at=None,
            client_avatar_url="https://static.avito.ru/i/128.png",
        )

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    ctx = {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}
    await client_enrich.enrich_client(ctx, conv_id)

    async with db_sessionmaker() as db:
        from app.models import Client as C

        fresh = await db.get(C, client_id)
        assert fresh is not None
        assert fresh.avatar_url == "https://static.avito.ru/i/128.png"


async def test_enrichment_keeps_the_photo_it_already_has(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Непустое фото не перезаписывается: иначе картинка мигала бы при каждом
    обогащении, а походы в Авито случались бы и там, где всё уже известно."""
    from app.integrations.avito.adapter import ChatInfo
    from app.models import Client
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=333444888)
    async with db_sessionmaker() as db:
        client_row = Client(
            id=uuid.uuid4(),
            channel="avito",
            external_id="902",
            name="Пётр",
            avatar_url="https://static.avito.ru/i/old.png",
            # Профиль отмечен спрошенным: с 02.09 обогащение ходит в Авито и
            # за ссылкой на профиль, и клиент, которого ещё не спрашивали,
            # делал бы поход законным. Здесь проверяется обратное — что при
            # известных полях похода нет вовсе.
            profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-ava2",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
            item_title="Известное объявление",
        )
        db.add(conv)
        await db.commit()
        conv_id, client_id = conv.id, client_row.id

    called = False

    async def _fake_fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        nonlocal called
        called = True
        raise AssertionError("походу в Авито тут неоткуда взяться: всё уже известно")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    ctx = {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}
    await client_enrich.enrich_client(ctx, conv_id)

    assert not called, "имя, объявление и фото известны — обогащению нечего добирать"
    async with db_sessionmaker() as db:
        from app.models import Client as C

        fresh = await db.get(C, client_id)
        assert fresh is not None
        assert fresh.avatar_url == "https://static.avito.ru/i/old.png"
