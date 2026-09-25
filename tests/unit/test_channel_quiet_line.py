"""Тишина на канале перестаёт быть обвинением подписке.

ЧТО БЫЛО. Индикатор вебхука считал ровно одно число — сколько времени прошло с
последнего события, — и при превышении зашитого порога писал:

    Webhook: ⚠️ событий нет 16 ч

с подсказкой о том, что подписка могла достаться другой системе. На канале
«Дамир» (пять обращений за неделю, последний диалог 11.08 в 18:23) это горело
всегда: шестнадцать часов тишины у него — обычная ночь плюс спокойное утро. С
интеграцией при этом было всё в порядке. Соседний «Тимофей» с дневным потоком
показывал «✓ в порядке» — разница между каналами была не в исправности, а в
количестве клиентов.

ЧТО СТАЛО (``app/services/channel_health.py``):

1. Тишина сравнивается не с часами на стене, а с ОБЫЧНЫМ РИТМОМ этого канала —
   медианой рабочей паузы за месяц. Рабочей: ночь и закрытая смена паузой не
   считаются, окно берётся из настроек «Распределение».
2. Прежде чем обвинить подписку, её СПРАШИВАЮТ у Авито. Чужой адрес — факт и
   красное; наш адрес — доказательство исправности, и тогда тишина остаётся
   нейтральной строкой «событий нет N ч, для этого канала это в пределах
   нормы».
3. Время сверки видно на карточке: «Сверка подписки 16:54, расхождений нет».

ПРОВЕРКА ЛОМАНИЕМ (делалась руками, каждая — по одной правке):

* вернуть суждение по абсолютному времени — заменить в ``webhook_health``
  условие ``quiet_minutes > threshold`` на ``silence_minutes > 240``: падают
  семь тестов, включая ``test_a_rare_channel_is_calm_at_night`` и
  ``test_a_rare_channel_is_calm_during_the_day`` (у «Дамира» снова вечное
  предупреждение), ``test_a_rare_channel_does_not_send_the_watchdog_to_avito``
  (сторож пошёл в чужой API из-за ночи) и сквозной тест карточки;
* убрать поправку на рабочие часы — считать паузу по календарю
  (``_work_minutes`` → разница в минутах): падают
  ``test_a_rare_channel_is_calm_at_night`` (шестнадцать часов ночи снова стали
  паузой) и ``test_the_rhythm_is_measured_in_working_hours``;
* поверить тишине без сверки — в ``check_quiet_channel_subscription`` поднять
  тревогу, не спрашивая Авито: падают три теста сторожа, первым —
  ``test_the_watchdog_asks_avito_before_blaming_the_subscription``;
* убрать порог ``QUIET_FACTOR`` и пол ``QUIET_FLOOR_MINUTES`` (сравнивать с
  самой медианой): падают пять тестов, включая
  ``test_a_pause_a_bit_longer_than_usual_is_still_neutral``.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.config import settings
from app.models import AvitoAccount, Client, Conversation, Message
from app.scheduler.jobs import watchdog
from app.services import avito_accounts as accounts_svc
from app.services import channel_health, crypto

#: Среда, 12 августа 2026, 10:30 по Москве — рабочий день только начался.
#: Рабочее окно по умолчанию 10:00–20:00 (``stats.work_start_hour``).
NOW = datetime(2026, 8, 12, 7, 30, tzinfo=UTC)

SUBSCRIPTIONS_URL = f"{settings.avito_api_base}/messenger/v1/subscriptions"
JIVO_URL = "https://jivo.example/avito/hook"


def msk(day_offset: float, hour: int, minute: int = 0) -> datetime:
    """Момент по московским стенным часам относительно дня :data:`NOW`."""
    base = (NOW + timedelta(days=day_offset)).astimezone(
        __import__("zoneinfo").ZoneInfo("Europe/Moscow")
    )
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(UTC)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """«Сейчас» фиксируем: расчёт идёт по рабочим часам МСК.

    Без этого тот же самый тест ночью и днём считал бы разные рабочие паузы —
    и падал бы через раз в зависимости от того, когда его запустили.
    """
    monkeypatch.setattr(channel_health, "_utcnow", lambda: NOW)
    return NOW


@pytest.fixture
def seed_events(db_sessionmaker: Any):
    """Обращения клиентов по каналу в заданные моменты времени.

    Ритм канала считается по СООБЩЕНИЯМ от клиента (``direction='in'`` и
    ``sender_type='client'``) — это то же определение входящего, что во всей
    системе, и то, что человек видел своими глазами.
    """

    async def _seed(account: AvitoAccount, moments: list[datetime]) -> None:
        async with db_sessionmaker() as session:
            client_row = Client(
                channel="avito", external_id=f"c-{_uuid.uuid4().hex[:8]}", name="Клиент"
            )
            session.add(client_row)
            await session.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{_uuid.uuid4().hex[:8]}",
                account_id=account.id,
                client_id=client_row.id,
                status="new",
                unread_count=0,
                last_message_at=max(moments) if moments else NOW,
            )
            session.add(conv)
            await session.flush()
            for moment in moments:
                session.add(
                    Message(
                        conversation_id=conv.id,
                        direction="in",
                        sender_type="client",
                        body="Здравствуйте",
                        attachments=[],
                        delivery_status="delivered",
                        created_at=moment,
                    )
                )
            await session.commit()

    return _seed


def rare_channel_events(*, last_event: datetime) -> list[datetime]:
    """«Дамир»: пять обращений в неделю. Раз в двое суток, днём."""
    return [msk(-day, 15) for day in range(28, 3, -2)] + [last_event]


def busy_channel_events(*, quiet_from_hour: int = 20) -> list[datetime]:
    """«Тимофей»: дневной поток — обращение каждые полчаса в рабочие часы.

    ``quiet_from_hour`` — с какого часа ПОСЛЕДНЕГО дня поток обрывается. Так
    задаётся настоящая остановка: у канала с получасовым ритмом четыре часа
    молчания в рабочее время — это уже новость.
    """
    moments: list[datetime] = []
    for day in range(6, 0, -1):
        end = quiet_from_hour if day == 1 else 20
        for hour in range(10, end):
            moments += [msk(-day, hour, 0), msk(-day, hour, 30)]
    return moments


async def _health(db, redis, account, *, now: datetime = NOW) -> channel_health.WebhookHealth:
    ctx = await channel_health.load_context(db, now=now)
    return await channel_health.webhook_health(db, redis, account, ctx=ctx)


async def _subscribed(redis: Any, account: AvitoAccount) -> None:
    """Подписка зарегистрирована — как после подключения канала."""
    await accounts_svc.set_webhook_state(
        redis, account.id, "ok", accounts_svc.webhook_url_for(account)
    )


async def _with_real_token(db_sessionmaker: Any, account: AvitoAccount) -> AvitoAccount:
    """Настоящий шифрованный токен: сверка его расшифровывает перед походом."""
    async with db_sessionmaker() as session:
        row = await session.get(AvitoAccount, account.id)
        assert row is not None
        row.access_token_enc = crypto.encrypt_token("live-token")
        await session.commit()
    return account


# =============================================================================
# 1. Редкий канал: тишина — это не поломка
# =============================================================================


async def test_a_rare_channel_is_calm_at_night(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """«Дамир»: 16 часов тишины — ночь, а не пропавшая подписка.

    Ровно эта карточка и висела с предупреждением: последнее обращение 11.08 в
    18:23, наутро «событий нет 16 ч» и текст про то, что подписку могла забрать
    другая система. В рабочих часах пауза здесь всего два часа — меньше
    обычной паузы этого канала.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-1, 18, 23)))

    health = await _health(db, redis, account)

    assert health.state in ("ok", "quiet"), health.message
    assert health.action is None
    assert health.silence_minutes is not None and health.silence_minutes > 15 * 60
    # Астрономических часов шестнадцать, рабочих — два: ночь тишиной не считаем.
    assert health.quiet_minutes is not None and health.quiet_minutes < 3 * 60


async def test_a_rare_channel_is_calm_during_the_day(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Четверо суток без обращений на «Дамире» — тоже норма, и так и сказано.

    Это второе состояние из трёх: нейтральное, но с объяснением. Именно оно
    отвечает на вопрос, который человек задаёт, глядя на пустой канал: «это
    сломалось или просто никто не пишет».
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-4, 15)))

    health = await _health(db, redis, account)

    assert health.state == "quiet", health.message
    assert health.reason == "quiet_but_normal"
    assert "в пределах нормы" in health.message
    assert health.action is None


async def test_a_pause_a_bit_longer_than_usual_is_still_neutral(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Порог — КРАТНОЕ превышение, а не «чуть больше обычного».

    Обычная пауза — величина со случайным разбросом: каждая десятая длиннее
    трёх медиан. Тревожить человека на каждой такой паузе значит звонить
    впустую раз в день, а это ровно то, от чего лечимся.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-4, 15)))

    health = await _health(db, redis, account)

    assert health.rhythm_minutes is not None and health.threshold_minutes is not None
    assert health.quiet_minutes is not None
    assert health.quiet_minutes > health.rhythm_minutes, "пауза длиннее обычной — так и задумано"
    assert health.quiet_minutes < health.threshold_minutes
    assert health.state == "quiet"


async def test_the_rhythm_is_measured_in_working_hours(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Ночь в ритм не входит — иначе «обычной» станет пауза, которой не бывает.

    Канал получает три обращения в день: в 10:00, 12:00 и 19:00. Рабочие
    паузы между ними — 120, 420 и 60 минут (ночь с 19:00 до 10:00 даёт всего
    час: с семи до восьми вечера, дальше смена закрыта). Медиана рабочих пауз
    — два часа.

    По календарю те же события дают 120, 420 и 900 минут, и медиана
    получается 420 — «норма» завышена втрое ночным простоем. С такой нормой
    настоящая дневная остановка на четыре часа выглядит обычным делом.
    """
    account = await make_avito_account(title="LP-Три-в-день")
    await _subscribed(redis, account)
    await seed_events(
        account,
        [msk(-day, hour) for day in range(14, 0, -1) for hour in (10, 12, 19)],
    )

    health = await _health(db, redis, account)

    assert health.rhythm_samples >= channel_health.RHYTHM_MIN_SAMPLES
    assert health.rhythm_minutes == 120, "медиана считается по календарю, а не по смене"


async def test_the_rhythm_of_a_busy_channel_is_its_own(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """У «Тимофея» обычная пауза — полчаса, и мерить его надо ею."""
    account = await make_avito_account(title="LP-Тимофей")
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events())

    health = await _health(db, redis, account)

    assert health.rhythm_minutes == 30
    # Пол порога всё равно держит: четыре медианы у бойкого канала — это
    # двухчасовая тревога, а измеренный дециль пауз — четыре часа.
    assert health.threshold_minutes == channel_health.QUIET_FLOOR_MINUTES


# =============================================================================
# 2. Настоящая остановка: пауза кратно больше обычной
# =============================================================================


async def test_a_channel_that_really_went_quiet_is_reported(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """«Тимофей» молчит пять рабочих часов при обычной паузе в полчаса.

    Столько же часов тишины у «Дамира» — норма. Разницу видит только сравнение
    с ритмом самого канала, и в этом весь смысл правки.
    """
    account = await make_avito_account(title="LP-Тимофей")
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events(quiet_from_hour=16))

    health = await _health(db, redis, account)

    assert health.state == "warning", health.message
    assert health.reason == "silence_abnormal"
    assert health.quiet_minutes is not None and health.threshold_minutes is not None
    assert health.quiet_minutes > health.threshold_minutes
    assert "дольше обычного" in health.message


async def test_without_a_known_rhythm_silence_never_raises_a_warning(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Только что подключённый канал сравнивать не с чем — значит молчим.

    Незнание не должно превращаться в тревогу: на новом канале любое число
    было бы выдумкой. Пропавшую подписку у таких каналов ловит ежедневный
    обход сторожа, который ходит по всем.
    """
    account = await make_avito_account(title="LP-Новый")
    await _subscribed(redis, account)
    await seed_events(account, [msk(-6, 12)])

    health = await _health(db, redis, account)

    assert health.rhythm_minutes is None
    assert health.threshold_minutes is None
    assert health.state == "quiet", health.message
    assert health.reason == "quiet_unknown_rhythm"
    assert health.action is None


# =============================================================================
# 3. Подписку сперва спрашивают, а потом обвиняют
# =============================================================================


async def test_a_foreign_subscription_is_the_only_red(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Чужой адрес — факт, и он не зависит от объёма обращений.

    Авито держит на аккаунт ровно одну подписку: чужой адрес означает, что
    канал обслуживает другая система, а обращения к нам не приходят вовсе.
    Это единственное, что заслуживает красного.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-1, 18, 23)))
    await channel_health._store_audit(
        redis,
        account.id,
        channel_health.SubscriptionAudit(
            checked_at=NOW - timedelta(minutes=10),
            result=channel_health.AUDIT_FOREIGN,
            foreign_urls=[JIVO_URL],
        ),
    )

    health = await _health(db, redis, account)

    assert health.state == "critical", health.message
    assert health.reason == "subscription_foreign"
    assert health.action == "rewebhook"
    assert "другая система" in health.message


async def test_the_card_says_when_the_subscription_was_checked(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """«Сверка подписки 16:54, расхождений нет» — время и итог прямо в строке.

    Это и есть то, чего на карточке не было: доказательство, что подписку
    кто-то проверял, и когда. Без него строка про тишину оставалась догадкой,
    а проверить её можно было только руками через журнал.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-4, 15)))
    checked = NOW - timedelta(minutes=20)
    await channel_health._store_audit(
        redis,
        account.id,
        channel_health.SubscriptionAudit(checked_at=checked, result=channel_health.AUDIT_OURS),
    )

    health = await _health(db, redis, account)

    assert health.check_result == "ours"
    assert health.checked_at == checked
    assert "расхождений нет" in health.message
    assert health.state == "quiet", "сверка подтвердила исправность — пугать нечем"


async def test_a_confirmed_subscription_outweighs_a_stale_registration_mark(
    db, redis, make_avito_account, seed_events, frozen_now
):
    """Пометка в Redis — память, ответ Авито — факт, и факт сильнее.

    Состояние подписки живёт в Redis и после перезапуска сервера у живого
    канала читается как «не зарегистрирован». Раньше карточка честно писала
    это слово и звала перерегистрировать работающую подписку. Сверка снимает
    вопрос: у Авито стоит наш адрес — значит всё в порядке.
    """
    account = await make_avito_account(title="LP-Тимофей")
    # webhook:state НЕ ставим: так выглядит живой канал после перезапуска.
    await seed_events(account, [*busy_channel_events(), msk(0, 10, 15)])
    await channel_health._store_audit(
        redis,
        account.id,
        channel_health.SubscriptionAudit(checked_at=NOW, result=channel_health.AUDIT_OURS),
    )

    health = await _health(db, redis, account)

    assert health.status == "not_registered", "пометка осталась как была"
    assert health.state == "ok", health.message
    assert health.action is None


# =============================================================================
# 4. Сторож: спросить Авито, а не гадать
# =============================================================================


@respx.mock
async def test_the_watchdog_asks_avito_before_blaming_the_subscription(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """КРИТЕРИЙ ПРИЁМКИ: тишина сама по себе больше никого не обвиняет.

    Канал замолчал дольше обычного — сторож идёт к Авито. Авито отвечает, что
    подписка наша, и тревоги НЕ возникает: молчание объяснилось. Раньше на этом
    месте поднималось критичное «Канал отобрали».
    """
    account = await make_avito_account(title="LP-Тимофей")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events(quiet_from_hour=16))
    route = respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": accounts_svc.webhook_url_for(account)}]}
        )
    )

    draft = await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)

    assert route.called, "сторож обвинил подписку, не спросив о ней"
    assert draft is None
    # Итог сверки сохранён — из него карточка и пишет «сверка …, расхождений нет».
    audit = await channel_health.read_audit(redis, account.id)
    assert audit.result == channel_health.AUDIT_OURS
    assert audit.checked_at == NOW


@respx.mock
async def test_the_watchdog_reports_a_subscription_taken_over(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """Замолчал и подписка чужая — вот теперь это критичная новость."""
    account = await make_avito_account(title="LP-Тимофей")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events(quiet_from_hour=16))
    respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(200, json={"subscriptions": [{"url": JIVO_URL}]})
    )

    draft = await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)

    assert draft is not None
    assert draft.kind == "webhook.lost"
    assert draft.severity == "critical"
    assert "LP-Тимофей" in draft.body


@respx.mock
async def test_a_rare_channel_does_not_send_the_watchdog_to_avito(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """КРИТЕРИЙ ПРИЁМКИ: у «Дамира» ночная тишина не поднимает вообще ничего.

    Ни тревоги, ни даже похода в чужой API: пауза в пределах обычной для этого
    канала, спрашивать не о чем. Проверяется именно факт отсутствия запроса —
    иначе девять каналов стучались бы в Авито каждые пять минут вечно.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-1, 18, 23)))
    route = respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(200, json={"subscriptions": [{"url": JIVO_URL}]})
    )

    draft = await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)

    assert draft is None
    assert not route.called, "сторож пошёл в Авито из-за обычной ночной паузы"


@respx.mock
async def test_the_watchdog_does_not_ask_avito_twice_in_a_row(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """Сверка — не чаще раза в полчаса, а проверки бегут каждые пять минут.

    Без этой отсечки замолчавший канал давал бы двенадцать запросов в час в
    чужой API — и так до тех пор, пока кто-нибудь не напишет.
    """
    account = await make_avito_account(title="LP-Тимофей")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events(quiet_from_hour=16))
    route = respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": accounts_svc.webhook_url_for(account)}]}
        )
    )

    await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)
    await watchdog.check_quiet_channel_subscription(db, redis, now=NOW + timedelta(minutes=5))

    assert route.call_count == 1


@respx.mock
async def test_a_restored_subscription_stops_the_alarm(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """Подписку вернули кнопкой — тревога замолкает сразу, а не через сутки.

    Найденный однажды чужой адрес нельзя ни забыть, ни запомнить навсегда:
    первое означало бы, что об отобранном канале напомнят только утром,
    второе — что после починки сторож ещё сутки кричит по старой записи.
    Решает сверка не старше получаса.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    # Тишина у этого канала обычная — поводом идёт только прошлая сверка.
    await seed_events(account, rare_channel_events(last_event=msk(-1, 18, 23)))
    await channel_health._store_audit(
        redis,
        account.id,
        channel_health.SubscriptionAudit(
            checked_at=NOW - timedelta(hours=2),
            result=channel_health.AUDIT_FOREIGN,
            foreign_urls=[JIVO_URL],
        ),
    )
    route = respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": accounts_svc.webhook_url_for(account)}]}
        )
    )

    draft = await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)

    assert route.called, "сторож поверил старой записи вместо того, чтобы переспросить"
    assert draft is None
    assert (await channel_health.read_audit(redis, account.id)).result == channel_health.AUDIT_OURS


@respx.mock
async def test_avito_being_silent_is_not_a_verdict(
    db, redis, make_avito_account, seed_events, db_sessionmaker, frozen_now
):
    """«Не смогли спросить» — не «подписки нет», и красным это не становится.

    Ложная тревога здесь стоила бы доверия ко всем остальным: о недоступном
    Авито скажут другие проверки.
    """
    account = await make_avito_account(title="LP-Тимофей")
    await _with_real_token(db_sessionmaker, account)
    await _subscribed(redis, account)
    await seed_events(account, busy_channel_events(quiet_from_hour=16))
    respx.post(SUBSCRIPTIONS_URL).mock(side_effect=httpx.ConnectError("нет сети"))

    draft = await watchdog.check_quiet_channel_subscription(db, redis, now=NOW)

    assert draft is None
    audit = await channel_health.read_audit(redis, account.id)
    assert audit.result == channel_health.AUDIT_UNKNOWN
    assert audit.lost is False


@respx.mock
async def test_the_daily_sweep_writes_down_its_result(
    db, redis, make_avito_account, db_sessionmaker, frozen_now
):
    """Ежедневный обход тоже оставляет след — иначе «сверка» была бы редкостью.

    Раньше ответ Авито жил ровно до конца цикла проверки и наружу не выходил:
    сторож знал, что подписка на месте, а карточка — нет.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _with_real_token(db_sessionmaker, account)
    respx.post(SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": accounts_svc.webhook_url_for(account)}]}
        )
    )

    assert await watchdog.check_webhook_lost(db, redis, now=NOW) is None

    audit = await channel_health.read_audit(redis, account.id)
    assert audit.result == channel_health.AUDIT_OURS
    assert audit.checked_at == NOW


# =============================================================================
# 5. То же самое глазами экрана
# =============================================================================


async def test_the_card_of_a_quiet_rare_channel_is_neutral(
    client, tokens, redis, make_avito_account, seed_events, frozen_now
):
    """Сквозная проверка: числа и состояние доезжают до карточки без потерь.

    Ответ ручки — единственный контракт с экраном, и проверять его надо
    целиком: правило, посчитанное на сервере и потерянное в схеме ответа, ничем
    не лучше отсутствующего.
    """
    account = await make_avito_account(title="LP-Дамир")
    await _subscribed(redis, account)
    await seed_events(account, rare_channel_events(last_event=msk(-4, 15)))

    resp = await client.get(
        "/api/v1/avito-accounts", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert resp.status_code == 200, resp.text
    card = next(item for item in resp.json()["items"] if item["id"] == str(account.id))

    webhook = card["webhook"]
    assert webhook["state"] == "quiet"
    assert webhook["action"] is None
    assert "в пределах нормы" in webhook["message"]
    assert webhook["rhythm_minutes"] and webhook["threshold_minutes"]
    assert webhook["rhythm_window_days"] == channel_health.RHYTHM_WINDOW_DAYS
    # Старые поля на месте: на них смотрит десктоп-клиент и прежние сборки.
    assert webhook["status"] == "ok"
    assert webhook["last_event_at"]

    # ⚠ ДВЕ ЧАСТИ ФРАЗЫ ДОЕЗЖАЮТ ДО ОТВЕТА (разбор интерфейса 13.08).
    #
    # Превращение датакласса в ответ здесь РУЧНОЕ, поле за полем
    # (`_account_out`, avito_connect.py): ни asdict, ни model_validate. Значит поле,
    # добавленное в датакласс, наружу само не поедет — и это самое лёгкое место
    # потерять правку целиком. Сервер посчитает заголовок, тесты сервиса будут
    # зелёными, а на экран приедет прежний длинный абзац.
    assert webhook["headline"] == "Событий нет, и для этого канала это в пределах нормы."
    assert "Без событий" in webhook["detail"]
    # Заголовок обязан быть короче — ради этого всё и затевалось.
    assert len(webhook["headline"]) < len(webhook["message"])

    token = card["token"]
    assert token["headline"] == "Токен активен."
    assert "Обновится автоматически" in token["detail"]
