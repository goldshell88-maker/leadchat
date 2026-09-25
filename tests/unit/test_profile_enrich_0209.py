"""Ссылка на профиль доезжает до карточки — и стоит нам ровно один вопрос.

⚠ ВТОРАЯ ПОЛОВИНА ПРОСЬБЫ ВЛАДЕЛЬЦА 02.09. Разбор адреса стережёт
`test_peer_profile_url_0209`; здесь — путь от Авито до карточки: кто идёт
спрашивать, что записывается и что уезжает в открытый экран.

⚠ И ГЛАВНОЕ — ЦЕНА. Условие «ссылки нет, сходи спроси» без памяти о попытке
означало бы поход в чужой API на КАЖДОЕ входящее сообщение, вечно, если Авито
ссылку не даёт. А он может не дать: форма поля первым лицом не проверена. Пятьсот
с лишним входящих в сутки — это пятьсот лишних обращений, которые делят окно с
доставкой ответов клиентам (08 §4.3), и ни одно ничего не приносит.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.avito.adapter import ChatInfo
from app.models import Client, Conversation

АДРЕС = "https://avito.ru/user/0a1b2c3d4e5f60718293a4b5c6d7e8f9/profile"


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        return True

    async def get(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def delete(self, *_a: Any) -> int:
        return 1


async def _посеять(
    db_sessionmaker: Any, account: Any, **client_kw: Any
) -> tuple[uuid.UUID, uuid.UUID]:
    async with db_sessionmaker() as db:
        client_row = Client(
            id=uuid.uuid4(),
            channel="avito",
            external_id=f"9234{uuid.uuid4().int % 10**8:08d}",
            name="Иван",
            avatar_url="https://static.avito.ru/i/ivan.png",
            **client_kw,
        )
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id=f"chat-prof-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
            item_title="Ремонт телевизоров",
        )
        db.add(conv)
        await db.commit()
        return conv.id, client_row.id


def _ответ(profile_url: str | None) -> Any:
    async def _fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        return ChatInfo(
            external_chat_id="chat-prof",
            client_external_id="923456789",
            client_name="Иван",
            item_title="Ремонт телевизоров",
            item_url=None,
            item_price=None,
            unread_count=0,
            has_unread=False,
            last_message_at=None,
            client_profile_url=profile_url,
        )

    return _fetch


async def test_ссылка_сохраняется_и_уезжает_в_открытый_экран(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Карточка не должна ждать перезагрузки страницы, чтобы показать кнопку."""
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666781)
    conv_id, client_id = await _посеять(db_sessionmaker, account)
    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _ответ(АДРЕС))

    redis = _FakeRedis()
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": redis}, conv_id
    )

    async with db_sessionmaker() as db:
        fresh = await db.get(Client, client_id)
        assert fresh is not None
        assert fresh.profile_url == АДРЕС
        assert fresh.profile_checked_at is not None, "отметка о вопросе обязана появиться"

    assert redis.published, "кадр обязан уехать: иначе кнопка появится только после F5"
    _, raw = redis.published[-1]
    assert json.loads(raw)["data"]["patch"]["client"]["profile_url"] == АДРЕС


async def test_пустой_ответ_тоже_записывается_как_вопрос(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """⚠ ЭТО И ЕСТЬ ОГРАНИЧИТЕЛЬ ЦЕНЫ, И ОН ВАЖНЕЕ САМОЙ ССЫЛКИ.

    Авито ответил без профиля. Ссылки нет — и не будет; но отметка «спрашивали»
    обязана появиться, иначе этот клиент будет гонять нас в чужой API на каждое
    своё сообщение до конца времён.
    """
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666782)
    conv_id, client_id = await _посеять(db_sessionmaker, account)
    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _ответ(None))

    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}, conv_id
    )

    async with db_sessionmaker() as db:
        fresh = await db.get(Client, client_id)
        assert fresh is not None
        assert fresh.profile_url is None
        assert fresh.profile_checked_at is not None, "пустой ответ — тоже знание"


async def test_второй_раз_в_авито_не_ходим(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Спросили однажды — больше не спрашиваем. Цена конечная."""
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666783)
    conv_id, _ = await _посеять(
        db_sessionmaker, account, profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC)
    )

    async def _нельзя(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("про профиль уже спрашивали — ходить незачем")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _нельзя)
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}, conv_id
    )


async def test_ссылку_не_затирают_повторным_обогащением(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Известную ссылку не трогаем: иначе она мигала бы при каждом обогащении."""
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666784)
    старая = "https://avito.ru/user/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/profile"
    conv_id, client_id = await _посеять(db_sessionmaker, account, profile_url=старая)
    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _ответ(АДРЕС))

    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}, conv_id
    )

    async with db_sessionmaker() as db:
        fresh = await db.get(Client, client_id)
        assert fresh is not None
        assert fresh.profile_url == старая


async def test_ручка_отдаёт_сохранённое_а_не_собранное() -> None:
    """API отдаёт то, что в базе. Из номера клиента ссылка не выводится."""
    from app.services.client_enrich import client_profile_url

    assert client_profile_url(Client(channel="avito", external_id="923456789")) is None
    assert (
        client_profile_url(Client(channel="avito", external_id="923456789", profile_url=АДРЕС))
        == АДРЕС
    )


async def test_открытие_диалога_спрашивает_про_профиль(
    client: Any, tokens: Any, db_sessionmaker: Any, make_avito_account: Any, monkeypatch: Any
) -> None:
    """⚠ У ЗАКРЫТОГО ДИАЛОГА ВХОДЯЩИХ БОЛЬШЕ НЕ БУДЕТ НИКОГДА.

    Обогащение добирает ссылку на входящем сообщении — для живой переписки
    этого хватает. Но карточку закрытого диалога как раз и открывают при
    разборе («кто это был»), а входящего там уже не случится: ссылка не
    появилась бы никогда. Жалоба владельца 02.09 пришла ровно с такой
    карточки — диалог закрыт 28 августа.
    """
    from app.api.routes import conversations as роут

    account = await make_avito_account(avito_user_id=555666790)
    conv_id, _ = await _посеять(db_sessionmaker, account)

    спрошено: list[uuid.UUID] = []

    async def _ловим(_redis: Any, cid: uuid.UUID) -> None:
        спрошено.append(cid)

    monkeypatch.setattr(роут, "enqueue_enrich_client", _ловим)
    r = await client.get(
        f"/api/v1/conversations/{conv_id}",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert r.status_code == 200, r.text
    assert спрошено == [conv_id], "открыли диалог — обязаны спросить про профиль"


async def test_второе_открытие_не_спрашивает(
    client: Any, tokens: Any, db_sessionmaker: Any, make_avito_account: Any, monkeypatch: Any
) -> None:
    """Повод гаснет после первого вопроса — иначе это запрос на каждое открытие."""
    from app.api.routes import conversations as роут

    account = await make_avito_account(avito_user_id=555666791)
    conv_id, _ = await _посеять(
        db_sessionmaker, account, profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC)
    )

    спрошено: list[uuid.UUID] = []

    async def _ловим(_redis: Any, cid: uuid.UUID) -> None:
        спрошено.append(cid)

    monkeypatch.setattr(роут, "enqueue_enrich_client", _ловим)
    r = await client.get(
        f"/api/v1/conversations/{conv_id}",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert r.status_code == 200
    assert спрошено == [], "про профиль уже спрашивали — второй раз не надо"


async def test_карточка_отличает_не_спрашивали_от_не_дали(
    client: Any, tokens: Any, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Признак, без которого подсказка в карточке врёт."""
    account = await make_avito_account(avito_user_id=555666792)
    свежий, _ = await _посеять(db_sessionmaker, account)
    спрошенный, _ = await _посеять(
        db_sessionmaker, account, profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC)
    )
    заголовки = {"Authorization": f"Bearer {tokens['manager']}"}

    r1 = await client.get(f"/api/v1/conversations/{свежий}", headers=заголовки)
    assert r1.json()["client"]["profile_checked"] is False
    r2 = await client.get(f"/api/v1/conversations/{спрошенный}", headers=заголовки)
    assert r2.json()["client"]["profile_checked"] is True
