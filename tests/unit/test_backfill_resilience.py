"""Загрузка истории Авито не должна умирать от одного плохого чата (#24).

ЧТО БЫЛО. Задача выкачивала чаты один за другим, и любое исключение на любом
из них уносило весь прогон. При тысяче чатов это означало: девятьсот
загрузились, сотый сломался, задача умерла — а в настройках канала осталось
вечное «идёт загрузка». Повторный запуск начинал бы всё заново.

Первопричиной был как раз промах по партициям: сообщение годовой давности не
находило, куда лечь. Её чинит `app/scheduler/partitions.py`, но одной починки
мало — устойчивость нужна и на будущее. Причина у сорвавшегося чата бывает
любая: битая карточка, вложение неизвестного вида, отказ Авито на середине.
Ни одна из них не является поводом бросить оставшиеся девятьсот девяносто
девять.

ЧТО ПРОВЕРЯЕМ ЗДЕСЬ: прогон доходит до конца, счёт ведётся честно, и человек
узнаёт про НЕзагруженное — молчаливая половина хуже честного «загружено 900,
не удалось 100».
"""

from typing import Any

import pytest

from app.services import avito_accounts as svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(770100)


def _chats(*chats: dict[str, Any], page_size: int = 100):
    """Список чатов как у Авито: ответ по смещению, а не очередь страниц.

    Прогон ходит по списку ДВАЖДЫ: сперва перепись (сколько всего чатов —
    знаменатель строки «загружено N из M»), потом сама загрузка. Фейк,
    отдающий страницы по одному разу, на второй проход возвращал бы пустоту,
    и любой тест «загрузилось столько-то» проходил бы при нулевой загрузке.
    """
    catalogue = list(chats)

    async def fake_call(fn, *_args, offset: int = 0, limit: int = page_size, **_kwargs):
        return catalogue[offset : offset + min(limit, page_size)]

    return fake_call


@pytest.fixture
def captured(monkeypatch) -> list[dict[str, Any]]:
    """Перехват уведомления админам вместо отправки в шину."""
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    return events


async def test_one_broken_chat_does_not_kill_the_run(
    monkeypatch, db_sessionmaker, redis, account, captured
) -> None:
    seen: list[str] = []

    async def fake_chat(_db, _redis, _client, _limiter, _account, raw_chat, _coverage, _plan):
        seen.append(raw_chat["id"])
        if raw_chat["id"] == "плохой":
            raise RuntimeError('no partition of relation "messages" found for row')
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "плохой"}, {"id": "b"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    # Дошли до конца страницы, а не остановились на сломанном чате.
    assert seen == ["a", "плохой", "b"]


async def test_report_names_what_did_not_load(
    monkeypatch, db_sessionmaker, redis, account, captured
) -> None:
    """Человек обязан узнать про НЕзагруженное.

    Если сказать только «загружено 2 диалога», администратор будет уверен, что
    вся переписка на месте, и узнает обратное от клиента — через неделю и с
    претензией.
    """

    async def fake_chat(_db, _redis, _client, _limiter, _account, raw_chat, _coverage, _plan):
        if raw_chat["id"] == "плохой":
            raise RuntimeError("битая карточка")
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "плохой"}, {"id": "b"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    (notice,) = captured
    assert notice["level"] == "warning"
    assert notice["title"] == "История загружена частично"
    assert "загружено 2" in notice["text"]
    assert "не удалось загрузить 1" in notice["text"]


async def test_clean_run_stays_quiet_and_positive(
    monkeypatch, db_sessionmaker, redis, account, captured
) -> None:
    """Без сбоев — прежний спокойный отчёт, без слова «частично»."""

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    (notice,) = captured
    assert notice["level"] == "info"
    assert notice["title"] == "История загружена"
    assert "не удалось" not in notice["text"]


async def test_progress_key_is_cleared_even_after_failures(
    monkeypatch, db_sessionmaker, redis, account, captured
) -> None:
    """Отметка прогресса снимается — иначе экран канала врёт «идёт загрузка».

    Это вторая половина той же беды: даже когда часть чатов не загрузилась,
    прогон ЗАКОНЧИЛСЯ, и карточка канала обязана это показать.
    """

    async def fake_chat(_db, _redis, _client, _limiter, _account, raw_chat, _coverage, _plan):
        raise RuntimeError("всё плохо")

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert await redis.get(f"backfill:{account.id}") is None
    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "idle"


async def test_run_stops_when_the_channel_is_switched_off_midway(
    monkeypatch, db, db_sessionmaker, redis, account, captured
) -> None:
    """Канал отключили во время прогона — дальше качать незачем.

    Выход обязан быть из ОБОИХ циклов: и по чатам текущей страницы, и по
    страницам. Иначе задача продолжит выкачивать отключённый канал до конца
    списка, а это тысячи обращений к Авито впустую и расход квоты.
    """
    seen: list[str] = []
    # Страница по два чата — чтобы выход был именно из ДВУХ циклов: одна
    # страница проверяла бы только внутренний.
    fake_call = _chats({"id": "a"}, {"id": "b"}, {"id": "c"}, page_size=2)

    async def fake_chat(inner_db, _redis, _client, _limiter, acc, raw_chat, _coverage, _plan):
        seen.append(raw_chat["id"])
        # Первый же чат ломается, а к моменту разбора канал уже отключён.
        acc.status = "disabled"
        await inner_db.commit()
        raise RuntimeError("сломался")

    monkeypatch.setattr(svc, "_avito_call", fake_call)
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert seen == ["a"], "после отключения канала прогон обязан остановиться"
