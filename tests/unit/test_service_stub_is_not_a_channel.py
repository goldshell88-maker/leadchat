"""Служебная заглушка не поднимает тревог и не притворяется каналом.

РАЗБОР ЖИВОЙ СИСТЕМЫ ОТ 11 АВГУСТА, ПУНКТ 4. На экране каналов у
``SMOKE-ACCOUNT`` стояли рядом: зелёная точка «работает», «Токен: активен» и
красное «Webhook: сбой», — а в колокольчике висело «Приём сообщений
остановился». Владелец справедливо решил, что канал сломан.

Ломаться там нечему: заглушку заводит ``app/cli.py seed-smoke``, и нужна она
ровно затем, что у служебного диалога ``SMOKE-CONV`` поле
``conversations.account_id`` объявлено NOT NULL. Токена Авито у неё нет — в
поле лежит строка ``smoke-account-has-no-avito-token``, — а срок действия
проставлен на десять лет вперёд, иначе запись не прошла бы собственных
проверок.

ДАЛЬШЕ СРАБОТАЛА АРИФМЕТИКА. Кнопка «Включить» проверяла ровно одно — жив ли
токен по дате, — и у заглушки он «жив» до 2036 года. Канал стал ``active``,
следом регистрация подписки сходила в Авито со строкой вместо токена и
записала ``webhook: failed``. А для сторожа появился «работающий канал», в
который не придёт ни одного обращения: «приём остановился» каждые пять минут,
вечно.

ЧТО ЗАПЕРТО ЗДЕСЬ:

* заглушку нельзя включить, обновить ей токен и подписать её на события —
  сервер отвечает 422 с ``reason=service_account``;
* сторож не считает её работающим каналом (``check_inbound_stalled``), не
  жалуется, что она не может отвечать (``check_channel_mute``), и не ходит
  за её подпиской в Авито (``check_webhook_lost``);
* список каналов отдаёт признак ``is_service``, без которого карточка не
  может отличить заглушку от канала.

ПРОВЕРКА ЛОМАНИЕМ (проделана):

* убрать ``_assert_not_a_stub(account)`` из ``enable_avito_account`` — падает
  ``test_a_stub_cannot_be_switched_on``;
* убрать ``_not_a_stub()`` из ``_working_channels_condition`` — падает
  ``test_a_stub_is_not_a_working_channel``;
* убрать ``_not_a_stub()`` из ``check_channel_mute`` — падает
  ``test_a_stub_is_never_accused_of_being_unable_to_answer``;
* убрать ``is_service`` из ``AvitoAccountOut`` — падает
  ``test_the_channel_list_marks_the_stub``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import AvitoAccount
from app.scheduler.jobs import watchdog

pytestmark = pytest.mark.anyio

# Вторник, 14:00 по Москве — глубина рабочего дня больше порога тишины
# (четыре часа), то есть проверка приёма до дела доходит. Тот же момент, что в
# tests/unit/test_watchdog.py: два разных «сейчас» для одной проверки — это
# два разных теста, которые кажутся одним.
WORK_NOW = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)


def _as(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def _make_stub(
    db: Any, *, status: str = "disabled", created_at: datetime | None = None
) -> AvitoAccount:
    """Заглушка ровно такая, какой её делает ``seed-smoke``.

    Срок токена — десять лет вперёд, и это не преувеличение ради теста, а
    дословно то, что пишет ``app/cli.py``. Именно эта дата и делала заглушку
    «работающим каналом» в глазах сторожа.

    ПОЛЕ ТОКЕНА ШИФРУЕТСЯ ПО-НАСТОЯЩЕМУ, ХОТЯ ВНУТРИ ЗАГЛУШКА. Так делает и
    ``seed-smoke`` (``encrypt_token(SMOKE_TOKEN_PLACEHOLDER)``), и без этого
    половина проверок ниже была бы зелёной по неверной причине: нечитаемый
    шифртекст роняет ``decrypt_token`` РАНЬШЕ похода в Авито, и «сторож не
    сходил за подпиской» выполнялось бы само собой, даже если бы запрет сняли.
    """
    from app.services import crypto

    token = crypto.encrypt_token("smoke-account-has-no-avito-token")
    account = AvitoAccount(
        title="SMOKE-ACCOUNT",
        avito_user_id=1,
        access_token_enc=token,
        refresh_token_enc=token,
        token_expires_at=datetime.now(UTC) + timedelta(days=3650),
        status=status,
        webhook_secret="whsec-smoke",
        is_service=True,
    )
    if created_at is not None:
        account.created_at = created_at
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


# --- кнопки карточки ---------------------------------------------------------


async def test_a_stub_cannot_be_switched_on(client, tokens, db) -> None:
    """«Включить» на заглушке — та самая кнопка, с которой всё началось."""
    stub = await _make_stub(db)

    response = await client.post(
        f"/api/v1/avito-accounts/{stub.id}/enable", headers=_as("admin", tokens)
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "service_account"
    # И в базе она осталась выключенной: отказ обязан быть отказом, а не
    # «включили и пожаловались».
    await db.refresh(stub)
    assert stub.status == "disabled"


async def test_a_stub_does_not_get_its_token_refreshed(client, tokens, db) -> None:
    """Обновлять нечего: в поле токена лежит строка-заполнитель.

    Заглушка взята ВКЛЮЧЁННОЙ намеренно — так она и выглядела на боевой
    системе после нажатия «Включить». Отказ обязан приходить и в этом
    состоянии, иначе проверка ловила бы только запрет для выключенных каналов,
    который здесь ни при чём.
    """
    stub = await _make_stub(db, status="active")

    response = await client.post(
        f"/api/v1/avito-accounts/{stub.id}/refresh-token", headers=_as("admin", tokens)
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "service_account"


async def test_a_stub_is_not_subscribed_to_events(client, tokens, db) -> None:
    """Та самая красная строка «Webhook: сбой» — след этого запроса."""
    stub = await _make_stub(db, status="active")

    response = await client.post(
        f"/api/v1/avito-accounts/{stub.id}/register-webhook", headers=_as("admin", tokens)
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "service_account"


async def test_a_real_channel_is_still_switched_on(client, tokens, db, make_avito_account) -> None:
    """Охранник от чрезмерного запрета.

    «Отказывать всем подряд» выглядело бы такой же зелёной правкой, как
    «отказывать заглушке»: три теста выше прошли бы оба раза.
    """
    account = await make_avito_account(880501, status="disabled")

    response = await client.post(
        f"/api/v1/avito-accounts/{account.id}/enable", headers=_as("admin", tokens)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "active"


# --- список каналов ----------------------------------------------------------


async def test_the_channel_list_marks_the_stub(client, tokens, db, make_avito_account) -> None:
    """Без признака в ответе карточка не может отличить заглушку от канала.

    Отличать её по названию (``SMOKE-ACCOUNT``) или по ``avito_user_id=1``
    значило бы размазать одно правило по всем читателям — а признак уже стоит
    в базе, и ставит его тот, кто заглушку и создал.
    """
    stub = await _make_stub(db)
    real = await make_avito_account(880502)

    response = await client.get("/api/v1/avito-accounts", headers=_as("admin", tokens))

    assert response.status_code == 200
    flags = {item["title"]: item["is_service"] for item in response.json()["items"]}
    assert flags[stub.title] is True
    assert flags[real.title] is False


# --- сторож ------------------------------------------------------------------


async def test_a_stub_is_not_a_working_channel(db, redis) -> None:
    """Главная тревога разбора: «приём остановился» из-за заглушки.

    Ни одного входящего сообщения в базе нет, а заглушка — единственный
    «включённый канал с живым токеном». Раньше отсюда следовало «работающий
    канал есть, обращений нет» — и так каждые пять минут, потому что
    обращений в заглушке не появится никогда.

    ДАТА ПОДКЛЮЧЕНИЯ ЗАДАНА НАЗАД, И БЕЗ НЕЁ ТЕСТ БЫЛ ЗЕЛЁНЫМ ВПУСТУЮ. Когда
    входящих нет вовсе, точкой отсчёта служит подключение самого старого
    работающего канала: свежесозданная заглушка давала «тишину» отрицательной
    длины, и проверка выходила, не дойдя до запрета. Снятие запрета такой тест
    не заметил бы — поймано ломанием.
    """
    await _make_stub(db, status="active", created_at=WORK_NOW - timedelta(days=2))

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None


async def test_a_real_channel_still_makes_the_silence_a_problem(
    db, redis, seed_conversation, db_sessionmaker
) -> None:
    """Второй охранник: молчать по любому поводу — тоже поломка сторожа."""
    from app.models import Message

    async with db_sessionmaker() as session:
        await session.execute(
            sa.update(Message)
            .where(Message.direction == "in")
            .values(created_at=WORK_NOW - timedelta(minutes=300))
        )
        await session.commit()

    draft = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "inbound.stalled"


async def test_a_stub_is_never_accused_of_being_unable_to_answer(db, redis) -> None:
    """«Канал не может отвечать клиентам» — критичное, и про заглушку оно ложь.

    Токен ей здесь просрочен нарочно: так выглядела бы заглушка, у которой
    десятилетний срок всё-таки вышел или была стёрта дата. Проверка перебирает
    все каналы, кроме выключенных вручную, поэтому и статус взят ``active``.
    """
    stub = await _make_stub(db, status="active")
    # Срок отсчитывается от WORK_NOW, а не от «сегодня»: проверка смотрит на
    # переданный момент, и токен, истёкший вчера по календарю машины, для неё
    # ещё действует. Тест был бы зелёным, не дойдя до запрета вовсе.
    stub.token_expires_at = WORK_NOW - timedelta(days=1)
    await db.commit()

    assert await watchdog.check_channel_mute(db, redis, now=WORK_NOW) is None


async def test_a_real_channel_without_a_token_is_still_reported(
    db, redis, make_avito_account
) -> None:
    """Охранник: критичную тревогу про настоящий канал запрет не съел."""
    account = await make_avito_account(880503)
    # Правим ЗАПРОСОМ, а не полем объекта: фикстура создаёт аккаунт в своей
    # сессии, и присваивание в чужой не доехало бы до базы — тест был бы
    # зелёным по неверной причине.
    await db.execute(
        sa.update(AvitoAccount)
        .where(AvitoAccount.id == account.id)
        .values(token_expires_at=WORK_NOW - timedelta(days=1))
    )
    await db.commit()

    draft = await watchdog.check_channel_mute(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "account.needs_reauth"
    assert account.title in draft.body


async def test_nobody_asks_avito_about_the_stub_subscription(db, redis, monkeypatch) -> None:
    """Спрашивать про подписку строкой вместо токена — гарантированный отказ.

    Раз в сутки, вечно, с записью «не смогли спросить» в лог. Проверяем не
    отсутствие тревоги (её и так не было бы — исключение проглатывается), а
    отсутствие САМОГО ПОХОДА: тревоги нет в обоих случаях, и по ней ошибку не
    отличить.

    Ради этого же токен заглушки шифруется по-настоящему (см. ``_make_stub``):
    с мусором вместо шифртекста поход обрывался бы на расшифровке, и проверка
    оставалась бы зелёной даже со снятым запретом.
    """
    await _make_stub(db, status="active")
    asked: list[str] = []

    class _Client:
        async def list_subscriptions(self, token: str) -> list[dict[str, str]]:
            asked.append(token)
            return []

    async def _fresh(*_a: Any, **_kw: Any) -> _Client:
        return _Client()

    from app.integrations.avito.client import AvitoClient

    monkeypatch.setattr(AvitoClient, "fresh", _fresh)

    assert await watchdog.check_webhook_lost(db, redis, now=WORK_NOW) is None
    assert asked == [], "сторож сходил в Авито за подпиской служебной заглушки"
