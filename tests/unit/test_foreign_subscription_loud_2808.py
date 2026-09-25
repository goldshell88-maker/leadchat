"""Вытеснение чужой подписки не проходит молча.

⚠ ЧТО ЭТО ЗА БЕДА. Авито держит на аккаунт РОВНО ОДНУ подписку на события.
Регистрация нашей снимает чужую — а чужая сегодня и есть работающий JivoChat, на
котором сидят тринадцать диспетчеров. Заслон `foreign_subscriptions` для этого и
написан, и его докстринг говорит прямо: «тринадцать диспетчеров перестают
получать обращения в ту же секунду».

Спрашивал об этом ровно ОДИН путь из четырёх — `connect_with_keys`
(«Подключить»), с подтверждением `takeover_confirmed`. Три остальных — возврат
OAuth, «Обновить подписку» и «Включить» — звали регистрацию напрямую, без
единого вопроса и без строки где-либо.

⚠ ПОЧЕМУ ЗДЕСЬ НЕ ОТКАЗ, А ГРОМКАЯ ЗАПИСЬ. Подтверждение перехвата живёт только
на сервере: во фронте `takeover_confirmed` не передаёт никто. Блокировка на трёх
новых путях стала бы тупиком — человек не смог бы ни включить канал, ни обновить
подписку. Поэтому вытеснение сделано ГРОМКИМ: строка уровня error попадает в
тревогу, и «канал молчит со вторника» перестаёт быть загадкой. Ставить
блокировку с подтверждением — решение владельца о порядке переезда.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import structlog

from app.core.config import settings
from app.models import AvitoAccount
from app.services import avito_accounts as svc
from app.services import crypto

pytestmark = pytest.mark.anyio

SUBSCRIPTIONS_URL = f"{settings.avito_api_base}/messenger/v1/subscriptions"
WEBHOOK_URL = f"{settings.avito_api_base}/messenger/v3/webhook"


@pytest.fixture
async def account(db):
    """Аккаунт с НАСТОЯЩИМ шифрованным токеном.

    Общая фикстура кладёт в токен заглушку `b"a"`, и `register_webhook` падает
    на расшифровке раньше, чем дойдёт до проверки подписок, — тест был бы
    зелёным по неверной причине.
    """
    row = AvitoAccount(
        title="LP-Тест",
        avito_user_id=990077,
        access_token_enc=crypto.encrypt_token("acc-live"),
        refresh_token_enc=crypto.encrypt_token("ref-live"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
        webhook_secret="whsec-test",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@respx.mock
async def test_чужая_подписка_попадает_в_журнал(account, redis) -> None:
    respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": "https://jivosite.example/hook", "version": "v3"}]}
        )
    )
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    with structlog.testing.capture_logs() as каптор_логов:
        assert await svc.register_webhook(account, redis) is True

    события = [з["event"] for з in каптор_логов]
    assert "webhook.foreign_subscription_replaced" in события, (
        "чужую подписку вытеснили молча — тринадцать диспетчеров останутся без обращений, "
        "и в журнале об этом не будет ни строки"
    )
    строка = next(з for з in каптор_логов if з["event"] == "webhook.foreign_subscription_replaced")
    assert строка["log_level"] == "error", "запись ниже уровня error не попадёт в тревогу"


@respx.mock
async def test_своя_подписка_тревоги_не_поднимает(account, redis) -> None:
    """Наш же адрес — не «чужая подписка»: перерегистрация обычное дело."""
    respx.post(SUBSCRIPTIONS_URL).mock(return_value=httpx.Response(200, json={"subscriptions": []}))
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    with structlog.testing.capture_logs() as каптор_логов:
        assert await svc.register_webhook(account, redis) is True

    события = [з["event"] for з in каптор_логов]
    assert "webhook.foreign_subscription_replaced" not in события


@respx.mock
async def test_отказ_самой_проверки_не_срывает_регистрацию(account, redis) -> None:
    """Проверка вспомогательная: не смогли спросить — подписываемся как раньше.

    Иначе одна недоступная ручка Авито заперла бы включение канала целиком, а
    это хуже, чем неизвестность про чужую подписку.
    """
    respx.post(SUBSCRIPTIONS_URL).mock(return_value=httpx.Response(500))
    маршрут = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert await svc.register_webhook(account, redis) is True
    assert маршрут.call_count == 1, "регистрация не состоялась из-за вспомогательной проверки"
