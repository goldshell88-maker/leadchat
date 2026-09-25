"""Фаза 1 доставки (чтение из базы) обязана повторяться, а не хоронить ответ.

⚠ РАЗБОР 03.09. Ошибки Авито в фазе 2 разобраны подробно и честно
повторяются. А фаза 1 — обычное чтение из базы — не была закрыта ничем:
заминка базы на секунду, и ARQ помечает задачу проваленной. Повтора у неё нет:
`max_tries` действует ТОЛЬКО на `Retry`, обычное исключение — это конец
(проверено по исходнику arq/worker.py).

Сообщение навсегда оставалось `pending`: ответ оператора клиенту не ушёл, и не
узнал об этом никто — сторож недоставленных смотрит на `failed`, а сторож
«клиент ждёт» молчит, потому что нажатие «Отправить» уже погасило ожидание.

⚠ ЭТИ ПРОВЕРКИ ПОВЕДЕНЧЕСКИЕ, А НЕ ПО ТЕКСТУ ИСХОДНИКА. Соседний файл
(`test_deliver_unexpected_error.py`) читает `inspect.getsource` — сегодня этот
приём трижды дал ложный результат, поэтому здесь задача РЕАЛЬНО вызывается.
"""

from __future__ import annotations

import uuid

import pytest
from arq import Retry

from app.workers import deliver

pytestmark = pytest.mark.anyio


class ПадающаяФабрика:
    """База недоступна: сессию не выдать вовсе."""

    def __call__(self):  # noqa: ANN204
        raise ConnectionError("connection refused")


async def test_заминка_базы_даёт_повтор_а_не_потерю(monkeypatch) -> None:
    """Первая попытка: обязан подняться Retry — иначе задача умрёт молча."""
    monkeypatch.setattr(deliver.log, "warning", lambda *a, **k: None)
    ctx = {"db_session_factory": ПадающаяФабрика(), "redis": object(), "job_try": 1}

    with pytest.raises(Retry):
        await deliver.deliver_message(ctx, uuid.uuid4())


async def test_на_исходе_попыток_сообщение_становится_красным(monkeypatch) -> None:
    """⚠ ВТОРАЯ ПОЛОВИНА ПРАВИЛА: вечный повтор так же плох, как потеря.

    Оператор обязан увидеть красное и нажать «Повторить». Тихо висящее
    `pending` не видит никто.
    """
    monkeypatch.setattr(deliver.log, "warning", lambda *a, **k: None)
    провалы: list[tuple] = []

    async def ловим(_ctx: dict, mid: uuid.UUID, err: str) -> None:
        провалы.append((mid, err))

    monkeypatch.setattr(deliver, "_fail", ловим)
    mid = uuid.uuid4()
    ctx = {
        "db_session_factory": ПадающаяФабрика(),
        "redis": object(),
        "job_try": deliver.MAX_TRIES,
    }

    await deliver.deliver_message(ctx, mid)  # без исключения: бюджет исчерпан

    assert провалы == [(mid, deliver.EXHAUSTED_ERROR)], (
        "на исходе попыток сообщение осталось pending — тихая пропажа ответа клиенту"
    )


async def test_отказ_записан_с_номером_фазы(monkeypatch) -> None:
    """Без номера фазы разбирать нечего: у фаз 1 и 2 разные причины и разный ремонт."""
    записи: list[dict] = []
    monkeypatch.setattr(deliver.log, "warning", lambda _e, **kw: записи.append(kw))
    ctx = {"db_session_factory": ПадающаяФабрика(), "redis": object(), "job_try": 1}

    with pytest.raises(Retry):
        await deliver.deliver_message(ctx, uuid.uuid4())

    assert записи and записи[-1].get("phase") == 1
    assert записи[-1].get("outcome") == "retry"
    assert "ConnectionError" in записи[-1].get("error", "")
