"""Сверки — вразнос, а не залпом; воркер берёт задачу без полусекундной паузы.

ЗАМЕР 06.09 (журнал воркера за 12–24 ч).
(а) Планировщик ставил все 34 сверки одной пачкой раз в 300 с. В первые 60 с
    после `scheduler.reconcile_enqueued` путь «вебхук → publish message:new»
    шёл p50 116 мс / p99 510 (n=417) против p50 25 / p99 78 вне порыва:
    входящее доходило до экрана в 4,6 раза дольше 18 % суток, а доставка
    ответов стартовала вдвое позже.
(б) ARQ опрашивал очередь раз в 0,5 с: от постановки deliver_message до старта
    p50 0,27 с, p90 0,49 (n=1421) — около 45 % всего времени до галочки
    «доставлено».

ЧТО СТЕРЕЖЁМ.
(1) i-я сверка ставится с отсрочкой i × 8 с: счёт задачи в `arq:queue` растёт
    на 8 000 мс между соседями, первая — без отсрочки.
(2) Имя задачи и дедупликация внутри интервала — прежние.
(3) `WorkerSettings.poll_delay == 0.1`, и это имя ARQ действительно читает.
(4) Шаг умещает боевые 34 канала в интервал сверки.

⚠ ДИВЕРСИИ (все прогнаны, все дали красный):
- убрать `_defer_by=...` из `enqueue_reconcile_all` или заменить на
  `0 * RECONCILE_STAGGER_STEP` — краснеет сторож шага;
- `_job_id=None` — краснеет сторож дедупликации (восемь задач вместо четырёх);
- переименовать `poll_delay` в `pool_delay` — краснеет сторож формы: значение
  на месте, но ARQ его не читает;
- `RECONCILE_STAGGER_STEP = timedelta(seconds=10)` — краснеет сторож размера.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from arq.connections import ArqRedis

from app.core.config import settings
from app.scheduler import main as mod
from app.services.messages import as_arq

#: Столько боевых каналов ходило в сверку 06.09 — размер, под который выбран шаг.
КАНАЛОВ_В_БОЮ_0609 = 34


@pytest.fixture
async def четыре_канала(make_avito_account: Any) -> None:
    for n in range(4):
        await make_avito_account(avito_user_id=700_000 + n, title=f"Канал {n}")


class ОчередьНаFakeredis:
    """Ровно та поверхность пула ARQ, которой касается планировщик, — с настоящим
    `ArqRedis.enqueue_job` (счёт, имя, WATCH-дедупликация) поверх fakeredis."""

    def __init__(self, redis: Any) -> None:
        self._redis = as_arq(redis)

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        return await ArqRedis.enqueue_job(self._redis, *args, **kwargs)


@pytest.fixture
def планировщик_на_fakeredis(
    monkeypatch: pytest.MonkeyPatch, redis: Any, db_sessionmaker: Any
) -> Any:
    monkeypatch.setattr(mod, "arq_pool", ОчередьНаFakeredis(redis))
    monkeypatch.setattr(mod.db_mod, "session_scope", db_sessionmaker)
    return redis


async def _счета_сверок(redis: Any) -> list[float]:
    задачи = await redis.zrange("arq:queue", 0, -1, withscores=True)
    return sorted(счёт for имя, счёт in задачи if имя.startswith("reconcile:"))


async def test_сверки_разнесены_по_восемь_секунд(
    четыре_канала: None, планировщик_на_fakeredis: Any
) -> None:
    """Счёт в `arq:queue` — момент старта в миллисекундах: первая сверка «сейчас»,
    каждая следующая на 8 000 позже предыдущей.

    Иначе параметр отсрочки принят и тихо выброшен, и залп вернулся.
    """
    сейчас_мс = time.time() * 1000
    await mod.enqueue_reconcile_all()

    счета = await _счета_сверок(планировщик_на_fakeredis)
    assert len(счета) == 4
    assert сейчас_мс - 1_000 <= счета[0] <= сейчас_мс + 1_000, (
        f"первая сверка встала на {счета[0] - сейчас_мс:.0f} мс от «сейчас», а обязана сразу"
    )
    шаги = [позже - раньше for раньше, позже in zip(счета, счета[1:], strict=False)]
    assert all(7_900 <= шаг <= 8_500 for шаг in шаги), (
        f"шаги между соседними сверками: {[round(ш) for ш in шаги]} мс, а просили 8 000"
    )


async def test_имя_задачи_и_дедупликация_прежние(
    четыре_канала: None, планировщик_на_fakeredis: Any
) -> None:
    """Разнос не имеет права менять имя `reconcile:{account}:{интервал}`:
    на нём держится «одна сверка канала на интервал», и повторный тик внутри
    того же интервала обязан не поставить ничего и не сдвинуть уже стоящие.
    """
    await mod.enqueue_reconcile_all()
    до = await _счета_сверок(планировщик_на_fakeredis)
    имена = [
        имя for имя, _ in await планировщик_на_fakeredis.zrange("arq:queue", 0, -1, withscores=True)
    ]
    интервал = int(time.time()) // settings.reconcile_interval_seconds
    assert all(имя.count(":") == 2 and имя.endswith(f":{интервал}") for имя in имена), имена

    await mod.enqueue_reconcile_all()
    после = await _счета_сверок(планировщик_на_fakeredis)
    assert после == до, "повторный тик внутри интервала поставил сверки заново"


def test_воркер_опрашивает_очередь_каждые_сто_миллисекунд() -> None:
    """Сторож формы, и вот почему только формы.

    Опрос живёт внутри `Worker.main()`: живой Redis, запущенный процесс, замер
    паузы между постановкой и стартом — то есть тест на сон, который в CI
    красен по погоде. Проверяемое здесь — что значение стоит и что стоит оно
    под именем, которое ARQ читает: настройки воркера собираются по совпадению
    имён с параметрами `Worker.__init__` (`arq.worker.get_kwargs`), и опечатка
    вроде `pool_delay` тихо оставила бы умолчание 0,5 с.
    """
    from arq.worker import get_kwargs

    from app.workers.main import WorkerSettings

    assert getattr(WorkerSettings, "poll_delay", None) == 0.1, (
        "опрос очереди снова раз в полсекунды — половина времени до «доставлено» "
        "уходит на ожидание, пока воркер заметит задачу"
    )
    # `vars()` — тот же словарь, который ARQ читает у класса настроек.
    assert get_kwargs(dict(vars(WorkerSettings))).get("poll_delay") == 0.1, (
        "имя настройки ARQ не читает: значение стоит, а воркер живёт по умолчанию"
    )


def test_шаг_умещает_боевые_каналы_в_интервал() -> None:
    """Последняя из 34 сверок обязана стартовать раньше следующего тика."""
    хвост = (КАНАЛОВ_В_БОЮ_0609 - 1) * mod.RECONCILE_STAGGER_STEP.total_seconds()
    assert хвост < settings.reconcile_interval_seconds, (
        f"хвост разноса {хвост:.0f} с уезжает за интервал {settings.reconcile_interval_seconds} с"
    )
    assert mod.RECONCILE_STAGGER_STEP.total_seconds() > 0, "нулевой шаг — это залп"
