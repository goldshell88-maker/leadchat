"""Приём сообщений от Авито не зависит от пула БД.

⚠ ЗАЧЕМ (замер боя 02.09). Шлюз делал одно чтение по первичному ключу — вроде бы
даром. Но чтение берёт соединение из пула в десять на процесс, а пул общий с
тяжёлыми выборками интерфейса. За восемь минут медленный поиск выбрал пул
целиком (166 ошибок «QueuePool limit reached»), и в ту же воронку утянуло приём:
из 566 вебхуков Авито 105 (18,6 %) закрылись по таймауту с его стороны.

Это ПОТЕРЯННЫЕ СООБЩЕНИЯ ЖИВЫХ КЛИЕНТОВ, и восстановить их нечем — до стрима они
не дошли. Причину (поиск и отсутствие потолка на запрос) правим отдельно, но
приём не должен зависеть от неё вовсе: у самого ценного, что делает система, не
должно быть общих ресурсов с показом экрана.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.api.routes import webhooks as шлюз
from app.core import trace


@pytest.fixture(autouse=True)
def чистая_память() -> Any:
    """Память процесса живёт между тестами — чистим до и после: и секреты, и
    срок следа (иначе итог зависел бы от порядка тестов)."""
    шлюз.забыть_секреты()
    trace.drop_cache()
    yield
    шлюз.забыть_секреты()
    trace.drop_cache()


async def test_повторный_вебхук_не_ходит_в_базу(
    client: Any, make_avito_account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА ФАЙЛА: на счастливом пути соединения к БД не берутся.

    Первый вебхук строку аккаунта читает — иначе секрет неоткуда взять. Все
    следующие обязаны обходиться памятью процесса: именно они составляют поток,
    и именно они терялись, когда пул был занят.
    """
    account = await make_avito_account(webhook_secret="whsec-test")
    адрес = f"/api/hooks/avito/{account.id}?secret=whsec-test"

    походов = {"get": 0, "execute": 0}
    настоящий_get = шлюз.AsyncSession.get
    настоящий_execute = шлюз.AsyncSession.execute

    async def считающий_get(self: Any, *a: Any, **kw: Any) -> Any:
        походов["get"] += 1
        return await настоящий_get(self, *a, **kw)

    async def считающий_execute(self: Any, *a: Any, **kw: Any) -> Any:
        # Запросы тоже считаются (проверка 24.09): чтение настроек следа шло
        # через `execute` на каждом вебхуке, а сторож считал только `get`.
        походов["execute"] += 1
        return await настоящий_execute(self, *a, **kw)

    monkeypatch.setattr(шлюз.AsyncSession, "get", считающий_get)
    monkeypatch.setattr(шлюз.AsyncSession, "execute", считающий_execute)

    for _ in range(5):
        r = await client.post(адрес, content=b'{"payload":{}}')
        assert r.status_code == 200, r.text

    # Первый вебхук читает строку аккаунта и срок следа; следующие четыре —
    # из памяти процесса (срок следа живёт `trace.CACHE_TTL_SECONDS`).
    assert походов == {"get": 1, "execute": 1}, (
        f"шлюз сходил в базу {походов} вместо одного чтения аккаунта и одного "
        "срока следа — значит приём по-прежнему делит пул с показом экрана"
    )


async def test_смена_секрета_подхватывается_сразу(
    client: Any, make_avito_account: Any, db_sessionmaker: Any
) -> None:
    """⚠ КЭШ НЕ ИМЕЕТ ПРАВА ОТВЕРГАТЬ НАСТОЯЩИЙ ВЕБХУК.

    В базу мы идём не по истечении срока, а при ЛЮБОМ несовпадении. Поэтому
    новый секрет срабатывает с первого раза, а не через пять минут: иначе смена
    секрета означала бы потерю сообщений — ровно то, от чего эта правка.
    """
    from app.models import AvitoAccount

    account = await make_avito_account(webhook_secret="старый")
    # Прогреваем память старым секретом.
    assert (await client.post(f"/api/hooks/avito/{account.id}?secret=старый")).status_code == 200

    async with db_sessionmaker() as s:
        строка = await s.get(AvitoAccount, account.id)
        строка.webhook_secret = "новый"
        await s.commit()

    r = await client.post(f"/api/hooks/avito/{account.id}?secret=новый", content=b"{}")
    assert r.status_code == 200, "новый секрет обязан работать сразу, а не через срок памяти"


async def test_чужой_секрет_по_прежнему_отвергается(client: Any, make_avito_account: Any) -> None:
    account = await make_avito_account(webhook_secret="whsec-test")
    # Прогреваем память верным секретом — чтобы проверка шла против кэша.
    assert (
        await client.post(f"/api/hooks/avito/{account.id}?secret=whsec-test")
    ).status_code == 200
    r = await client.post(f"/api/hooks/avito/{account.id}?secret=не-тот", content=b"{}")
    assert r.status_code == 403


async def test_незнакомый_аккаунт_память_не_заводит(client: Any) -> None:
    """Иначе адрес со случайным идентификатором растил бы память процесса."""
    чужой = uuid.uuid4()
    r = await client.post(f"/api/hooks/avito/{чужой}?secret=что-нибудь", content=b"{}")
    assert r.status_code == 403
    assert чужой not in шлюз._СЕКРЕТЫ


async def test_упавший_след_не_стоит_вебхука(
    client: Any, make_avito_account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ДИАГНОСТИКА, СТОЯЩАЯ ДОСТАВКИ, — НЕ ДИАГНОСТИКА.

    `trace.refresh` идёт в базу и в час исчерпанного пула способен держать ответ
    до тридцати секунд. Авито столько не ждёт. Сообщение к этому моменту уже в
    стриме, значит терять ответ из-за следа нельзя ни при каких условиях.
    """
    account = await make_avito_account(webhook_secret="whsec-test")

    async def падает(_db: Any) -> bool:
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(шлюз.trace, "refresh", падает)
    r = await client.post(f"/api/hooks/avito/{account.id}?secret=whsec-test", content=b"{}")
    assert r.status_code == 200, "вебхук потерян из-за диагностики"
