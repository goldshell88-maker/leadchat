"""Служебная заглушка в сверку не ставится.

НАЙДЕНО В ЖУРНАЛЕ БОЕВОЙ СИСТЕМЫ 12 августа: 832 строки ошибок за двое суток, и
все — один служебный канал SMOKE-ACCOUNT. Его одноразовый refresh-токен сгорел
(имитатор Авито держит такие токены в памяти и теряет при перезапуске), сверка
падала трассировкой каждые несколько минут и падала бы вечно: канал остаётся
`active`, планировщик ставит задачу заново каждый интервал.

ЧЕМ ЭТО ОПАСНО, ХОТЯ КАНАЛ И НЕНАСТОЯЩИЙ. Шум хоронит сигнал. На этой же
системе 112 ложных тревог закрыли собой два настоящих провала резервной копии,
и заметили их неделю спустя — разбор 11 августа. Восемьсот строк трассировок в
журнале обработчика делают ровно это.

И проверять на заглушке нечего: боевые каналы обновляют доступ СВОИМИ ключами
(`client_credentials`), а заглушка — одноразовым refresh-токеном. Сверка на ней
гоняла ветку, которой в бою нет вовсе.

Сторож исключает служебные каналы тем же признаком и по той же причине
(`watchdog._real_channels_condition`) — сверка была единственным местом, где
про заглушку забыли.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from app.models import AvitoAccount


@pytest.fixture
async def каналы(db_sessionmaker: Any, make_avito_account: Any) -> dict[str, Any]:
    боевой = await make_avito_account(avito_user_id=880011, title="Боевой")
    заглушка = await make_avito_account(avito_user_id=1, title="SMOKE-ACCOUNT")
    async with db_sessionmaker() as db:
        row = await db.get(AvitoAccount, заглушка.id)
        assert row is not None
        row.is_service = True
        await db.commit()
    return {"боевой": боевой.id, "заглушка": заглушка.id}


async def test_в_сверку_попадают_только_боевые(
    db_sessionmaker: Any, каналы: dict[str, Any]
) -> None:
    """Отбор планировщика повторён здесь ОДИН В ОДИН.

    Звать сам `enqueue_reconcile_all` пришлось бы с живым ARQ и глобальным
    пулом; проверяется же ровно условие выборки — и оно обязано совпадать с
    тем, что стоит в `app/scheduler/main.py`.
    """
    async with db_sessionmaker() as db:
        ids = (
            (
                await db.execute(
                    sa.select(AvitoAccount.id).where(
                        AvitoAccount.status == "active",
                        AvitoAccount.is_service.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert каналы["боевой"] in ids
    assert каналы["заглушка"] not in ids, (
        "заглушка снова попала в сверку — журнал зальёт трассировками"
    )


def test_отбор_в_планировщике_совпадает_с_проверенным() -> None:
    """Сторож на само условие: тест выше проверяет копию, а не оригинал.

    Копия могла бы разойтись с планировщиком молча — тогда тест остался бы
    зелёным, а заглушка вернулась бы в сверку. Поэтому здесь читается сам файл.
    """
    import pathlib

    src = pathlib.Path("app/scheduler/main.py").read_text(encoding="utf-8")
    начало = src.index("async def enqueue_reconcile_all")
    тело = src[начало : src.index("\nasync def ", начало + 10)]
    assert "AvitoAccount.is_service.is_(False)" in тело, (
        "из отбора планировщика пропал признак служебного канала"
    )
