"""Две беды, найденные на боевой системе 11 августа вечером.

ПЕРВАЯ — ГОРОД ПРОСТАВЛЯЛСЯ НЕ ВЕЗДЕ. Задача обогащения ставится, только когда
объявления НЕТ. А сверка и загрузка истории приносят событие уже С объявлением —
такой диалог задачи не получал, и город у него не появлялся никогда. На боевой
базе так набрался 21 диалог со ссылкой вида ``/bryansk/...`` и пустым городом.
Показывался он всё равно (сборка ответа разбирает ссылку на лету), но в базе
снимка не было — значит по нему нельзя ни отобрать, ни посчитать.

ВТОРАЯ — «ОБНОВИТЬ ТОКЕН» ВОСКРЕШАЛА ВЫКЛЮЧЕННЫЙ КАНАЛ. ``apply_token_response``
ставил ``status="active"`` безусловно. Поймано на боевом: аккаунт-заглушка smoke,
создаваемый строго выключенным («никто не „включит“ заглушку случайно» —
app/cli.py), оказался в списке работающих. Для настоящего канала цена выше:
«Отключить» необратимо стирает переписку, и человек, выключивший канал, увидел
бы, что тот снова принимает обращения.

Проверка ломанием: верните безусловный ``account.status = "active"`` — падает
``test_refresh_does_not_resurrect_a_disabled_channel``; уберите ``item_city_slug``
из вставки диалога — падает ``test_city_is_set_when_the_item_arrives_with_the_event``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.avito.adapter import InboundEvent
from app.models import AvitoAccount, Conversation
from app.services import avito_accounts as acc_svc
from app.services.client_enrich import city_slug_of


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


# --- город при записи ссылки --------------------------------------------------


def test_city_slug_of_reads_the_link() -> None:
    assert city_slug_of("https://avito.ru/bryansk/predlozheniya_uslug/remont_123456") == "bryansk"
    assert city_slug_of("https://avito.ru/items/3311902447") is None
    assert city_slug_of(None) is None
    # Подделка домена городом не считается — иначе чужая ссылка красила бы
    # карточку клиента настоящим городом.
    assert city_slug_of("https://avito.ru.evil.com/moskva/x_1") is None


async def test_city_is_set_when_the_item_arrives_with_the_event(
    db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Событие пришло С объявлением — задачи обогащения не будет, город нужен сразу."""
    from app.services import inbound as inbound_svc

    account = await make_avito_account(avito_user_id=771122334)
    event = InboundEvent(
        external_chat_id="chat-city-at-write",
        external_message_id="m-city-1",
        author_id=880011,
        account_user_id=account.avito_user_id,
        text="Здравствуйте",
        created_at=datetime.now(UTC),
        client_name="Пётр",
        item_title="Ремонт принтеров и МФУ",
        item_url="https://avito.ru/bryansk/predlozheniya_uslug/remont_printerov_777888999",
        item_price=None,
    )
    async with db_sessionmaker() as db:
        await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)
        conv = (
            await db.execute(
                __import__("sqlalchemy")
                .select(Conversation)
                .where(Conversation.external_chat_id == "chat-city-at-write")
            )
        ).scalar_one()
        assert conv.item_city_slug == "bryansk", "город обязан проставляться в точке записи ссылки"


# --- выключенный канал --------------------------------------------------------


@pytest.mark.parametrize(
    ("status_before", "status_after"),
    [
        ("active", "active"),
        ("needs_reauth", "active"),  # починка канала — законный переход
        ("disabled", "disabled"),  # выключенный остаётся выключенным
    ],
)
def test_refresh_does_not_resurrect_a_disabled_channel(
    status_before: str, status_after: str
) -> None:
    account = AvitoAccount(
        id=uuid.uuid4(),
        title="LP-Проверка",
        avito_user_id=999000111,
        access_token_enc=b"x",
        refresh_token_enc=b"y",
        token_expires_at=datetime.now(UTC) - timedelta(hours=1),
        status=status_before,
        webhook_secret="whsec",
    )
    acc_svc.apply_token_response(account, {"access_token": "fresh", "expires_in": 86400})
    assert account.status == status_after
    # Токен обновился в любом случае: он мог понадобиться для снятия подписки.
    assert account.token_expires_at > datetime.now(UTC)
