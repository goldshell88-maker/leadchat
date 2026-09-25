"""ОТКАЗ НА СТРАНИЦЕ СПИСКА ЧАТОВ НЕ УНОСИТ ПРОГОН КАНАЛА.

⚠ ЖАЛОБА ВЛАДЕЛЬЦА 28.08: «сгрузка аккаунтов иногда срывается».

ЗАМЕР БОЯ в тот же день, за сорок минут активной загрузки: Авито не ответил
58 раз. Из них 57 пришлись на историю ОТДЕЛЬНЫХ чатов
(`/messenger/v3/accounts/N/chats/ЧАТ/messages/`) — там отказ изолирован
`_backfill_chat` и стоит одного чата из тысячи. А ОДИН пришёлся на страницу
списка (`/messenger/v2/accounts/N/chats`), и вот он уносил весь прогон:
исключение выходило из цикла страниц наружу, общий `except` ставил пометку
«сорвалась» и бросал дальше. Загруженное оставалось в базе, но канал вставал до
следующего тика сторожа.

Один отказ на 58 — это примерно раз в сорок минут при полной загрузке. Ровно то,
что владелец видел как «иногда срывается».

ЧТО ПРОВЕРЯЕМ: сетевой отказ страницы повторяется, а на исчерпании повторов
заход кончается ЧЕСТНО — с сохранённым ходом работы и поставленным
продолжением, — вместо смерти прогона. И отдельно: то, что бедой ЯВЛЯЕТСЯ
(отказ в доступе), бедой и остаётся.
"""

from typing import Any

import pytest

from app.integrations.avito.errors import AvitoApiError, AvitoAuthError, AvitoUnavailable
from app.services import avito_accounts as svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(771000)


@pytest.fixture
def тихо(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    return events


@pytest.fixture
def поставленные(monkeypatch) -> list[tuple]:
    ставили: list[tuple] = []

    async def fake_enqueue(account_id, depth=svc.DEFAULT_HISTORY_DEPTH, *, dedupe=True):
        ставили.append((account_id, depth, dedupe))

    monkeypatch.setattr(svc, "enqueue_backfill", fake_enqueue)
    return ставили


@pytest.fixture(autouse=True)
def без_пауз(monkeypatch):
    """Повторы не должны превращать набор в ожидание восьми секунд."""

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(svc.asyncio, "sleep", fake_sleep)


def _страницы(monkeypatch, поведение) -> list[int]:
    """Фейк списка чатов: `поведение(номер_попытки)` возвращает список или бросает."""
    попытки: list[int] = []

    async def fake_call(fn, *_args, offset: int = 0, limit: int = 100, **_kwargs):
        попытки.append(offset)
        return поведение(len(попытки), offset)

    monkeypatch.setattr(svc, "_avito_call", fake_call)
    return попытки


async def test_a_flaky_page_is_retried_not_fatal(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Один сетевой отказ — повторяем и идём дальше, как ни в чём не бывало."""

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    # Спотыкаемся на ПЕРВОЙ странице ЗАГРУЗКИ (перепись к этому моменту уже
    # прошла): без повтора заход кончился бы здесь, не загрузив ни одного
    # диалога, и «История загружена» не прозвучала бы вовсе.
    состояние = {"перепись_кончилась": False, "споткнулись": False}

    def поведение(_попытка: int, offset: int):
        if not состояние["перепись_кончилась"]:
            if offset == 0:
                return [{"id": "a"}, {"id": "b"}]
            состояние["перепись_кончилась"] = True
            return []
        if not состояние["споткнулись"]:
            состояние["споткнулись"] = True
            raise AvitoUnavailable()
        return [{"id": "a"}, {"id": "b"}] if offset == 0 else []

    _страницы(monkeypatch, поведение)
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert await redis.get(svc._failed_key(account.id)) is None, (
        "одиночный сетевой отказ страницы снова помечает загрузку сорвавшейся"
    )
    assert поставленные == [], (
        "заход прервался на одном сетевом отказе вместо повтора — загрузка тысячи "
        "чатов превратится в цепочку из сотен заходов"
    )
    assert тихо and тихо[-1]["title"] == "История загружена"


async def test_a_dead_page_ends_the_slice_and_queues_more(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Повторы исчерпаны — заход кончается честно, а не падает.

    Ход работы остаётся точкой возобновления, продолжение ставится само.
    Пометки «сорвалась» нет: работа не сорвана, она приостановлена.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    def поведение(попытка: int, offset: int):
        # Перепись проходит, а загрузка упирается в глухую сеть.
        if попытка <= 2:
            return [{"id": "a"}] if попытка == 1 else []
        raise AvitoUnavailable()

    _страницы(monkeypatch, поведение)
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert await redis.get(svc._failed_key(account.id)) is None, (
        "заход, упёршийся в сеть, помечен как сорвавшийся — человек полезет "
        "нажимать «Повторить» там, где всё продолжится само"
    )
    assert поставленные, "продолжение не поставлено: канал встанет до тика сторожа"
    assert await redis.get(svc._progress_key(account.id)) is not None, (
        "ход работы стёрт — продолжение начнёт с нуля"
    )
    assert тихо == [], "конец захода разбудил человека тревогой на ровном месте"


async def test_page_retries_are_bounded(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Повторов ровно столько, сколько объявлено, — а не бесконечно.

    Упереться в глухую сеть навсегда значит держать слот воркера, в котором
    стоит доставка ответов клиентам.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    def поведение(_попытка: int, _offset: int):
        raise AvitoUnavailable()

    попытки = _страницы(monkeypatch, поведение)
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    # Перепись и загрузка — два места, у каждого свой заход по три попытки.
    assert len(попытки) <= 2 * svc.CHATS_PAGE_TRIES


async def test_access_denied_is_still_a_real_failure(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """ОТКАЗ В ДОСТУПЕ ОСТАЁТСЯ БЕДОЙ, и повторять его нечем.

    Токен не приняли даже после авто-рефреша: это не рябь сети, а сломанное
    подключение канала. Промолчи мы здесь — человек не узнал бы, что история
    не грузится вовсе, и ждал бы её неделями.
    """

    def поведение(_попытка: int, _offset: int):
        raise AvitoAuthError("Авито: токен не принят (403)", status=403)

    попытки = _страницы(monkeypatch, поведение)

    with pytest.raises(AvitoAuthError):
        await svc.backfill_account(
            {"db_session_factory": db_sessionmaker, "redis": redis}, account.id
        )

    assert len(попытки) == 1, (
        "отказ в доступе повторяли — это трата квоты на заведомо мёртвое. "
        "Ловит его общее правило «любой 4xx — наверх»: отдельной ветки под "
        "AvitoAuthError нет, она была бы мёртвым кодом"
    )
    assert await redis.get(svc._failed_key(account.id)) is not None, (
        "сломанное подключение не помечено — карточка канала будет молчать"
    )


async def test_platform_ceiling_still_ends_the_run_calmly(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """400 на дальней странице — потолок площадки, прежняя спокойная ветка.

    Проверка стоит здесь, потому что новый помощник страницы легко мог бы
    проглотить 4xx вместе с сетевыми отказами и превратить «дошли до конца» в
    «повторяем трижды и ставим продолжение» — то есть в бесконечную цепочку.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    def поведение(_попытка: int, offset: int):
        if offset == 0:
            return [{"id": "a"}]
        raise AvitoApiError("Авито: чаты -> HTTP 400", status=400)

    _страницы(monkeypatch, поведение)
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert await redis.get(svc._failed_key(account.id)) is None
    assert поставленные == [], "потолок площадки превратился в бесконечную цепочку заходов"
    assert тихо and "Глубже Авито не отдаёт" in тихо[-1]["text"]
