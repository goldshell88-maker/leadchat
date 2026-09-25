"""Потолок ответа Redis обязан быть больше блокирующего чтения (03.09).

⚠ ЭТОТ ФАЙЛ НАПИСАН ПОСЛЕ БОЕВОЙ АВАРИИ, А НЕ ДО НЕЁ.

Утром 03.09 таймауты Redis задали явно и общими — три секунды на команду, с
доводом «наши команды это XADD, INCR и HGETALL, миллисекунды по существу».
Довод был неверен: цикл приёма сообщений от клиентов читает поток командой,
которая ЖДЁТ НАРОЧНО — `XREADGROUP ... BLOCK 5000`.

Пять секунд ожидания против трёх секунд сокета — и в бою каждый холостой заход
кончался `TimeoutError: Timeout reading from redis:6379`, переподключением и
секундной паузой. 163 трассировки за двадцать минут. Сообщения доходили
(пришедшее в окно чтения возвращается сразу), но пришедшее сразу после разрыва
ждало следующего захода, а журнал заливало так, что настоящую ошибку в нём было
бы уже не найти.

Правило простое и проверяемое: потолок сокета > самой длинной блокирующей
команды процесса. Ниже оно и стоит.
"""

from __future__ import annotations

import pytest

from app.core import redis as redis_mod
from app.core.config import settings
from app.workers import inbound

pytestmark = pytest.mark.anyio


def test_потолок_воркера_больше_блокирующего_чтения() -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА ФАЙЛА, И ОНА ЛОВИТ ИМЕННО ТУ АВАРИЮ.

    Запас нужен не «для красоты»: между истечением BLOCK на стороне Redis и
    ответом по сети проходит время, и потолок, равный ожиданию, срабатывал бы
    на границе через раз.
    """
    потолок = redis_mod.socket_timeout_for("worker")
    ожидание = inbound.READ_BLOCK_MS / 1000

    assert потолок > ожидание, (
        f"сокет сдаётся ({потолок} с) раньше, чем отвечает Redis ({ожидание} с): "
        "цикл приёма сообщений будет рвать соединение на каждом холостом заходе"
    )
    assert потолок >= ожидание * 1.5, (
        f"запас всего {потолок - ожидание:.1f} с — на границе будет срабатывать через раз"
    )


def test_у_веб_процесса_потолок_остаётся_коротким() -> None:
    """⚠ И ЭТО ВТОРАЯ ПОЛОВИНА ПРАВИЛА, БЕЗ НЕЁ ПЕРВАЯ ВРЕДНА.

    Самый простой способ «починить» аварию — поднять потолок всем. Тогда
    замолчавший Redis веб-запрос заметил бы через десять секунд вместо трёх,
    а за веб-запросом сидит человек, и всё это время занято ещё и соединение
    к базе. Ровно от этого потолок и заводили: 105 потерянных вебхуков 02.09
    начались с занятого пула.
    """
    assert redis_mod.socket_timeout_for("api") == settings.redis_socket_timeout_seconds
    assert redis_mod.socket_timeout_for("api") < redis_mod.socket_timeout_for("worker")


async def test_воркер_действительно_берёт_свой_потолок(monkeypatch) -> None:
    """⚠ «ПОДКЛЮЧЕНО ЛИ» — ОТДЕЛЬНЫЙ ВОПРОС К «ПРАВИЛЬНО ЛИ».

    Проверки выше говорят, что число верное. Они ничего не говорят о том, что
    воркер его БЕРЁТ: достаточно забыть `component="worker"` в одной строке
    старта — и всё вернётся, а проверки выше останутся зелёными. Сегодня этот
    же класс дефекта («написано, но не подключено») поймали дважды, оба раза
    после выкатки.

    Старт воркера поднимает движок БД, пул ARQ и фоновую задачу приёма — всё
    это здесь не нужно и мешает. Подменяем ровно их, а от `get_client`
    забираем аргумент.
    """
    import asyncio

    from app.workers import main as worker_main

    запрошено: list[str] = []

    class Заглушка:
        def __getattr__(self, _name: str):  # noqa: ANN202
            async def ничего(*_a: object, **_k: object) -> None:
                return None

            return ничего

    def подмена_клиента(component: str = "api") -> Заглушка:
        запрошено.append(component)
        return Заглушка()

    async def пул(*_a: object, **_k: object) -> Заглушка:
        return Заглушка()

    async def ничего(*_a: object, **_k: object) -> None:
        return None

    async def петля(_ctx: dict) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_main.redis_mod, "get_client", подмена_клиента)
    monkeypatch.setattr(worker_main, "create_pool", пул)
    monkeypatch.setattr(worker_main.db_mod, "init_engine", lambda *a, **k: None)
    monkeypatch.setattr(worker_main.db_mod, "get_session_factory", lambda: None)
    monkeypatch.setattr(worker_main.avito_app, "seed_process", ничего)
    monkeypatch.setattr(worker_main.leadbot, "seed_process", ничего)
    monkeypatch.setattr(worker_main, "inbound_consumer_loop", петля)

    ctx: dict = {}
    try:
        await worker_main.startup(ctx)
    finally:
        задача = ctx.get("inbound_task")
        if задача is not None:
            задача.cancel()

    assert запрошено == ["worker"], (
        "старт воркера просит клиента Redis без component='worker' — "
        f"потолок сокета вернётся к трём секундам и авария повторится (запрошено: {запрошено})"
    )
