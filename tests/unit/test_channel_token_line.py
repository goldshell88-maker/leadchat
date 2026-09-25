"""Строка «Токен …» на карточке канала перестаёт пугать без причины.

ЧТО БЫЛО. Карточка получала одно поле — ``token_expires_at``, — и решала по
нему сама: осталось меньше суток → жёлтая строка со знаком «⚠️»:

    ⚠️ Токен истекает через 23 ч — 13.08.2026 18:07. Обновление идёт само за
    два часа до срока; если не сработает, ответы перестанут уходить.

Токен Авито живёт СУТКИ. Значит эта строка горела жёлтым на всех каналах
двадцать три часа из двадцати четырёх, то есть практически всегда. Люди читали
её как аварию и жали «Обновить токен» руками каждый день — при том что
перевыпуск по постоянным ``client_id``/``client_secret`` идёт сам, каждые
тридцать минут, начиная за два часа до срока.

Цена такого индикатора не в лишних нажатиях. Индикатор, который горит всегда,
не означает ничего — и в тот день, когда обновление действительно сломается,
жёлтую строку никто не заметит, потому что она там была вчера и позавчера.

ЧТО СТАЛО (``app/services/channel_health.py``): три состояния вместо одного, и
граница жёлтого привязана не к «мало времени осталось», а к «автообновление
уже должно было пройти и не прошло». Плюс наружу поехало то, чего экрану
неоткуда узнать: получалось ли обновление вообще и когда в последний раз.

ПРОВЕРКА ЛОМАНИЕМ (делалась руками, каждая — по одной правке):

* ``TOKEN_WARN_AHEAD = timedelta(hours=24)`` — то самое прежнее поведение:
  падают ``test_23_hours_left_is_the_calm_state`` и оба граничных теста;
* убрать ветку ``if journal.failures:`` из ``token_health`` — падают
  ``test_a_failed_auto_refresh_turns_the_line_yellow`` и
  ``test_three_failures_in_a_row_are_a_breakdown``;
* снять ``note_refresh_ok``/``note_refresh_failed`` из ``refresh_tokens`` —
  падает ``test_the_refresh_writes_down_how_it_went``: карточка снова не знает
  ни одного факта о работе автоматики.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.models import AvitoAccount
from app.services import avito_accounts as accounts_svc
from app.services import channel_health


async def _set_token_expiry(db_sessionmaker: Any, account_id: Any, *, left: timedelta) -> None:
    async with db_sessionmaker() as session:
        await session.execute(
            update(AvitoAccount)
            .where(AvitoAccount.id == account_id)
            .values(token_expires_at=datetime.now(UTC) + left)
        )
        await session.commit()


async def _card(client: Any, tokens: dict[str, str], account_id: Any) -> dict[str, Any]:
    """Карточка канала так, как её видит экран настроек."""
    resp = await client.get(
        "/api/v1/avito-accounts", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert resp.status_code == 200, resp.text
    return next(item for item in resp.json()["items"] if item["id"] == str(account_id))


# =============================================================================
# Три состояния
# =============================================================================


async def test_23_hours_left_is_the_calm_state(client, tokens, make_avito_account, db_sessionmaker):
    """ГЛАВНЫЙ ТЕСТ ЭТОГО ФАЙЛА: 23 часа из 24 — это норма, а не тревога.

    Ровно это число стояло на боевых каналах круглые сутки и подавалось
    жёлтым с восклицательным знаком. Ответ обязан сказать «нейтрально» и
    назвать время, когда система возьмётся за токен сама, — тогда экрану
    нечего додумывать, а человеку нечего нажимать.
    """
    account = await make_avito_account()
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=23))

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "ok", token
    assert token["reason"] is None
    assert token["action"] is None, "кнопка «Обновить токен» звала нажимать без повода"
    assert "Обновится автоматически" in token["message"]
    # Сроком карточка больше не пугает, но и не скрывает его: он в полях.
    assert token["expires_in_minutes"] == pytest.approx(23 * 60, abs=2)
    assert token["auto_refresh_at"] is not None


@pytest.mark.parametrize(
    ("left_minutes", "expected"),
    [
        # Порог — полтора часа: два часа минус один обход планировщика. Разбор
        # числа — в :data:`channel_health.TOKEN_WARN_AHEAD`.
        (23 * 60, "ok"),  # то самое «истекает через 23 ч»
        (121, "ok"),  # автообновление ещё даже не начиналось
        (100, "ok"),  # начало окна обновления: планировщик идёт каждые 30 мин
        (91, "ok"),  # последняя спокойная минута
        (90, "warning"),  # полная попытка была и не сработала
        (20, "warning"),
    ],
)
async def test_the_warning_boundary_is_a_missed_auto_refresh(
    make_avito_account, left_minutes: int, expected: str
):
    """Граница порога, и почему она именно там.

    Плановое обновление начинается за два часа до срока и повторяется каждые
    тридцать минут. Пока не прошла хотя бы одна полная попытка, «мало времени»
    означает «планировщик ещё не добежал» — и жёлтый в этом окне давал бы
    полчаса ложной тревоги каждые сутки на каждом канале.

    Считается тут не через ручку, а прямой вызов с зафиксированным «сейчас»:
    на границе важна ровно одна минута, а между записью срока в базу и сборкой
    ответа проходит доля секунды — и та самая минута теряется при округлении.
    """
    now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    account = await make_avito_account()
    account.token_expires_at = now + timedelta(minutes=left_minutes)

    token = channel_health.token_health(account, channel_health.RefreshJournal(), now=now)

    assert token.state == expected, token.message
    if expected == "warning":
        assert token.action == "refresh_token"
        assert token.reason == "expiring"
    else:
        assert token.action is None, "кнопка зовёт нажимать без повода"


async def test_an_expired_token_says_what_it_costs(
    client, tokens, make_avito_account, db_sessionmaker
):
    """Авария — это когда ответы клиентам УЖЕ не уходят, и так и написано."""
    account = await make_avito_account()
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=-1))

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "critical"
    assert token["reason"] == "expired"
    assert "ответы клиентам не уходят" in token["message"]
    assert token["action"] == "refresh_token"
    # Ноль, а не «минус шестьдесят»: отрицательный остаток на экране — загадка.
    assert token["expires_in_minutes"] == 0


# =============================================================================
# Автообновление: его результат теперь наблюдаем
# =============================================================================


async def test_a_failed_auto_refresh_turns_the_line_yellow(
    client, tokens, redis, make_avito_account, db_sessionmaker
):
    """СРОК ЗДЕСЬ НИ ПРИ ЧЁМ — жёлтым делает провалившаяся попытка.

    До суток ещё двадцать три часа, но последнее автообновление не прошло.
    Это и есть тот единственный случай, ради которого строка вообще должна
    быть жёлтой: дальше само не наладится, а человек может нажать кнопку.
    """
    account = await make_avito_account()
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=23))
    await channel_health.note_refresh_failed(redis, account.id, reason="Авито не ответил")

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "warning"
    assert token["reason"] == "refresh_failed"
    assert token["last_refresh_ok"] is False
    assert token["last_error"] == "Авито не ответил"
    assert token["action"] == "refresh_token"


async def test_three_failures_in_a_row_are_a_breakdown(
    client, tokens, redis, make_avito_account, db_sessionmaker
):
    """Одна неудача — сетевая икота, три подряд — сломанная автоматика.

    Полтора часа безуспешных попыток означают, что до истечения токена само
    ничего не наладится, и ждать больше нечего.
    """
    account = await make_avito_account()
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=20))
    for _ in range(channel_health.TOKEN_FAILURES_CRITICAL):
        await channel_health.note_refresh_failed(redis, account.id, reason="Авито не ответил")

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "critical"
    assert token["reason"] == "refresh_broken"
    assert token["failures"] == channel_health.TOKEN_FAILURES_CRITICAL
    assert "перестанут уходить" in token["message"], "не названа цена — только факт"


async def test_the_card_shows_when_the_refresh_last_worked(
    client, tokens, redis, make_avito_account, db_sessionmaker
):
    """«Когда последний раз получилось» снимает большую часть вопросов.

    Без этого числа единственным наблюдаемым фактом был срок истечения — и
    любой срок выглядел угрозой, потому что доказательства работающей
    автоматики не было вовсе.
    """
    account = await make_avito_account()
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=23))
    when = datetime.now(UTC) - timedelta(minutes=40)
    await channel_health.note_refresh_ok(redis, account.id, now=when)

    token = (await _card(client, tokens, account.id))["token"]

    assert token["last_refresh_ok"] is True
    assert token["last_refresh_at"] is not None
    assert token["failures"] == 0
    assert token["state"] == "ok"


async def test_a_success_clears_the_failure_streak(redis, make_avito_account):
    """Успех обнуляет счётчик: канал, который починился, красным не остаётся."""
    account = await make_avito_account()
    await channel_health.note_refresh_failed(redis, account.id, reason="Авито не ответил")
    await channel_health.note_refresh_failed(redis, account.id, reason="Авито не ответил")
    assert (await channel_health.read_journal(redis, account.id)).failures == 2

    await channel_health.note_refresh_ok(redis, account.id)

    journal = await channel_health.read_journal(redis, account.id)
    assert journal.failures == 0
    assert journal.last_attempt_ok is True


async def test_an_empty_journal_never_paints_the_card(make_avito_account):
    """Redis перезапустили — карточка обязана остаться нейтральной.

    Журнал живёт в Redis намеренно (схему базы в этом спринте правит соседняя
    группа), и его потеря — обычное дело. Правило простое: незнание никогда не
    становится тревогой. Иначе перезапуск Redis красил бы все девять каналов.
    """
    account = await make_avito_account()
    account.token_expires_at = datetime.now(UTC) + timedelta(hours=23)

    token = channel_health.token_health(account, channel_health.RefreshJournal())

    assert token.state == "ok"
    assert token.last_refresh_at is None
    assert token.last_refresh_ok is None, "«попыток не было» и «попытка провалилась» — разные вещи"


# =============================================================================
# Стык с самим обновлением: журнал заполняет тот, кто обновляет
# =============================================================================


class _FakeRedisJournal:
    """Лок из ``refresh_tokens`` плюс настоящее хранилище журнала.

    Свой класс, а не fakeredis: ``refresh_tokens`` берёт лок через
    ``set(..., nx=True)``, и здесь важно, что запись журнала идёт ПОСЛЕ него и
    именно тем же клиентом.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str = "1", **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


async def _ready(value: Any) -> Any:
    return value


def _client_returning(data: dict[str, Any]) -> Any:
    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            return data

    return _Client()


def _client_raising(exc: BaseException) -> Any:
    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            raise exc

    return _Client()


@pytest.fixture
def keys_account(db_sessionmaker: Any):
    """Канал на своих ключах — как оба боевых аккаунта заказчика."""

    async def _make() -> AvitoAccount:
        from app.services import crypto

        async with db_sessionmaker() as session:
            account = AvitoAccount(
                title="LP-Ключи",
                avito_user_id=100000001,
                access_token_enc=crypto.encrypt_token("access"),
                refresh_token_enc=crypto.encrypt_token("refresh"),
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
                status="active",
                webhook_secret="whsec-test",
                client_id="cid-permanent",
                client_secret_enc=crypto.encrypt_token("csecret-permanent"),
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account

    return _make


async def test_the_refresh_writes_down_how_it_went(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, keys_account: Any
):
    """Сквозная проверка: удачное обновление отмечается, неудачное — считается.

    Без этой записи карточка не знает о работе автоматики НИЧЕГО, и всё
    остальное в этом файле держится на пустоте: состояние «внимание, последняя
    попытка не прошла» просто никогда бы не наступило.
    """
    from app.integrations.avito.errors import AvitoUnavailable

    account = await keys_account()
    redis = _FakeRedisJournal()

    monkeypatch.setattr(
        accounts_svc.AvitoClient,
        "fresh",
        staticmethod(
            lambda _db: _ready(
                _client_returning({"access_token": "a", "refresh_token": "r", "expires_in": 86400})
            )
        ),
    )
    async with db_sessionmaker() as session:
        row = await session.get(AvitoAccount, account.id)
        assert row is not None
        assert await accounts_svc.refresh_tokens(row, session, redis) is True

    journal = await channel_health.read_journal(redis, account.id)
    assert journal.last_ok_at is not None, "успешное обновление осталось незамеченным"
    assert journal.failures == 0

    # Теперь Авито не отвечает: канал остаётся живым (это временная беда), но
    # неудача обязана попасть в журнал — иначе карточка о ней не узнает.
    monkeypatch.setattr(
        accounts_svc.AvitoClient,
        "fresh",
        staticmethod(lambda _db: _ready(_client_raising(AvitoUnavailable()))),
    )
    async with db_sessionmaker() as session:
        row = await session.get(AvitoAccount, account.id)
        assert row is not None
        assert await accounts_svc.refresh_tokens(row, session, redis) is False

    journal = await channel_health.read_journal(redis, account.id)
    assert journal.failures == 1
    assert journal.last_error == "Авито не ответил"
    # Время последнего УСПЕХА не стирается неудачей: именно оно отвечает на
    # вопрос «когда всё в последний раз было хорошо».
    assert journal.last_ok_at is not None


async def test_a_competing_refresh_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, keys_account: Any
):
    """Занятый лок — не неудача, и в счётчик он попасть не должен.

    Обновление идёт из планировщика и из кнопки одновременно; тот, кто не
    получил лок, ничего не пробовал. Считать это «неудачной попыткой» значило
    бы красить карточку красным на ровном месте — тремя нажатиями подряд.
    """
    account = await keys_account()
    redis = _FakeRedisJournal()
    await redis.set(f"lock:token:{account.id}", "1")

    async with db_sessionmaker() as session:
        row = await session.get(AvitoAccount, account.id)
        assert row is not None
        assert (
            await accounts_svc.refresh_tokens(row, session, redis, wait_for_competitor=False)
            is False
        )

    assert (await channel_health.read_journal(redis, account.id)).failures == 0


# =============================================================================
# Каналы, которых это не касается
# =============================================================================


async def test_a_disabled_channel_is_not_blamed_for_its_own_switch(
    client, tokens, make_avito_account, db_sessionmaker
):
    """Выключенный канал не обновляется — и красным за это не красится.

    Планировщик обходит только активные каналы (и на ключах — из
    ``needs_reauth``), поэтому у выключенного токен истекает всегда. Красная
    строка «ответы не уходят» была бы обвинением человеку за его же решение.
    """
    account = await make_avito_account(status="disabled")
    await _set_token_expiry(db_sessionmaker, account.id, left=timedelta(hours=-40))

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "ok"
    assert token["reason"] == "disabled"
    assert token["action"] is None


async def test_the_service_stub_says_it_has_no_token(client, tokens, db_sessionmaker):
    """У заглушки регрессионного набора токена Авито нет вовсе.

    В поле лежит строка-заполнитель со сроком на десять лет вперёд, и «Токен
    активен, до 2036 года» читалось как рабочий канал — ровно так её и
    прочитали на боевой системе 11 августа.
    """
    async with db_sessionmaker() as session:
        account = AvitoAccount(
            title="SMOKE (служебный)",
            avito_user_id=1,
            access_token_enc=b"smoke-account-has-no-avito-token",
            refresh_token_enc=b"smoke",
            token_expires_at=datetime.now(UTC) + timedelta(days=3650),
            status="disabled",
            webhook_secret="smoke",
            is_service=True,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)

    token = (await _card(client, tokens, account.id))["token"]

    assert token["state"] == "ok"
    assert token["reason"] == "service_stub"
    assert "заглушка" in token["message"]
