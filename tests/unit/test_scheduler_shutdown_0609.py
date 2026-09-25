"""Планировщик останавливается штатно, а не через SIGKILL (замер боя 05.09).

ЧТО БЫЛО. `main()` жил на `await asyncio.Event().wait()` с пометкой «SIGTERM
гасит контейнер». В контейнере python — PID 1, а ядро не доставляет PID 1
сигнал без обработчика; свой Python ставит только на SIGINT. Итог на КАЖДОЙ
остановке: `docker stop` ждал 10 с и бил SIGKILL (exit 137). APScheduler не
завершался, задача в полёте рвалась на середине, соединения к базе и Redis
бросались незакрытыми, выкатка была длиннее на десять секунд. Ещё десять
секунд отдавал nginx: его graceful-сигнал ждал открытые WebSocket'ы, которые
всё равно рвутся при пересоздании api.

ЧТО СТЕРЕЖЁМ.
(1) Обработчики на SIGTERM и SIGINT стоят и ведут к `scheduler.shutdown(
    wait=False)` и снятию ожидания; повторный сигнал безвреден.
(2) После настоящего SIGTERM `main()` возвращается и закрывает пулы в порядке
    воркера: очередь → Redis → база.
(3) В compose у планировщика `init: true` (окно до установки обработчика
    закрывает tini), у nginx — короткий `stop_grace_period` при штатном
    graceful-сигнале.

⚠ ДИВЕРСИЯ: убрать `loop.add_signal_handler` из `_install_stop_handlers` —
краснеют оба сторожа про сигналы. Первый краснеет ДО отправки сигнала: он
проверяет, что обработчик стоит, прежде чем слать SIGTERM самому себе, иначе
диверсия убивала бы процесс pytest целиком, а не один тест. Поменять порядок
закрытия пулов — краснеет сторож порядка. Убрать `init: true` у планировщика
или `stop_grace_period` у nginx — краснеет соответствующий сторож compose.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import re
import signal
from typing import Any, cast

import pytest

from app.scheduler import main as mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"


class FakeScheduler:
    """Ровно та поверхность APScheduler, которой касается `main()`."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.shutdown_calls: list[bool] = []

    def start(self) -> None:
        self.started.set()

    def get_jobs(self) -> list[Any]:
        return []

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls.append(wait)


def _wire(monkeypatch: pytest.MonkeyPatch, scheduler: FakeScheduler, order: list[str]) -> None:
    """Всё тяжёлое из `main()` — заглушки; закрытия пишут своё имя в `order`."""

    class FakePool:
        async def aclose(self) -> None:
            order.append("arq")

    async def _pool(*_a: Any, **_k: Any) -> FakePool:
        return FakePool()

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _close_redis() -> None:
        order.append("redis")

    async def _dispose() -> None:
        order.append("engine")

    monkeypatch.setattr(mod, "configure_logging", lambda **_k: None)
    monkeypatch.setattr(mod, "init_sentry", lambda *_a: None)
    monkeypatch.setattr(mod.db_mod, "init_engine", lambda **_k: None)
    monkeypatch.setattr(mod.redis_mod, "init_client", lambda: None)
    monkeypatch.setattr(mod, "create_pool", _pool)
    monkeypatch.setattr(mod, "ensure_message_partitions", _noop)
    monkeypatch.setattr(mod, "heartbeat", _noop)
    monkeypatch.setattr(mod, "build_scheduler", lambda: scheduler)
    monkeypatch.setattr(mod.redis_mod, "close_client", _close_redis)
    monkeypatch.setattr(mod.db_mod, "dispose_engine", _dispose)
    monkeypatch.setattr(mod, "arq_pool", None)
    # Догон пропущенных прогонов (24.09) читает пульс из Redis — здесь его нет.
    monkeypatch.setattr(mod.redis_mod, "get_client", lambda *_a, **_k: object())
    monkeypatch.setattr(mod.catch_up, "previous_heartbeat", _noop)

    async def _nothing_missed(*_a: Any, **_k: Any) -> list[str]:
        return []

    monkeypatch.setattr(mod.catch_up, "catch_up_missed", _nothing_missed)


async def test_real_sigterm_stops_main_and_closes_the_pools_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настоящий SIGTERM самому себе: `main()` возвращается, пулы закрыты."""
    scheduler = FakeScheduler()
    order: list[str] = []
    _wire(monkeypatch, scheduler, order)
    loop = asyncio.get_running_loop()

    task = asyncio.create_task(mod.main())
    try:
        await asyncio.wait_for(scheduler.started.wait(), 5)
        # СНАЧАЛА — что обработчик стоит, и только потом сигнал. Без этой
        # проверки диверсия «убрать add_signal_handler» убивала бы SIGTERM'ом
        # процесс pytest вместо одного красного теста.
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL, (
            "обработчика SIGTERM нет — в бою это 10 с ожидания и SIGKILL"
        )
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
    finally:
        task.cancel()  # если сигнал не дошёл — не оставлять main() висеть
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    assert scheduler.shutdown_calls == [False]
    assert order == ["arq", "redis", "engine"], "порядок воркера: очередь → Redis → база"


async def test_both_signals_are_wired_and_a_repeat_is_harmless() -> None:
    """SIGINT (Ctrl+C) наравне с SIGTERM; второй сигнал не зовёт shutdown снова.

    Повторный `shutdown()` у APScheduler — SchedulerNotRunningError прямо в
    цикле событий, а Ctrl+C дважды нажимают все.
    """
    registered: dict[signal.Signals, tuple[Any, tuple[Any, ...]]] = {}

    class FakeLoop:
        def add_signal_handler(self, sig: signal.Signals, callback: Any, *args: Any) -> None:
            registered[sig] = (callback, args)

    scheduler = FakeScheduler()
    stop = asyncio.Event()

    loop = cast(asyncio.AbstractEventLoop, FakeLoop())
    mod._install_stop_handlers(loop, cast(Any, scheduler), stop)

    assert set(registered) == {signal.SIGTERM, signal.SIGINT}
    callback, args = registered[signal.SIGTERM]
    callback(*args)
    callback(*args)
    assert stop.is_set()
    assert scheduler.shutdown_calls == [False]


def _service_block(name: str) -> list[str]:
    """Строки службы `name` из compose без комментариев — до следующего ключа."""
    inside = False
    block: list[str] = []
    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^  {re.escape(name)}:\s*$", raw):
            inside = True
            continue
        if inside and re.match(r"^ {0,2}\S", raw):
            break
        if inside and not raw.strip().startswith("#"):
            block.append(raw)
    assert block, f"в {COMPOSE.name} нет службы {name}"
    return block


def test_scheduler_runs_under_init() -> None:
    """tini как PID 1: SIGTERM доходит до python и до того, как встал обработчик."""
    block = _service_block("scheduler")

    assert any(re.match(r"^    init:\s*true\s*$", line) for line in block), (
        "у scheduler нет init: true — SIGTERM к PID 1 без обработчика уходит в никуда"
    )


def test_nginx_stop_grace_is_short_and_the_signal_stays_graceful() -> None:
    """Открытые WS не закрываются за любой срок — ждать их нечего, api всё равно
    пересоздаётся; но сигнал остаётся SIGQUIT, чтобы HTTP-запросы в полёте
    доиграли."""
    block = _service_block("nginx")

    grace = [m for line in block if (m := re.match(r"^    stop_grace_period:\s*(\d+)s\s*$", line))]
    assert grace, "у nginx нет stop_grace_period — по умолчанию 10 с ожидания WS и SIGKILL"
    assert int(grace[0].group(1)) <= 5, "срок обязан быть короче умолчания docker (10 с)"
    signals = [line for line in block if re.match(r"^    stop_signal:", line)]
    assert all("SIGQUIT" in line for line in signals), "stop_signal обязан оставаться graceful"
