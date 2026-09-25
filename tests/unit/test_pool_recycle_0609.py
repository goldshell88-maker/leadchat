"""Соединение к базе живёт два часа, а не полчаса (замер 06.09).

Полчаса давали ~80 холодных соединений в час на все пулы, и первый
партиционированный запрос на каждом стоил 40–62 мс одного планирования: кэш
каталога по 28 партициям `messages` и кэш подготовленных выражений asyncpg
живут в соединении и умирают вместе с ним.

Долгий срок безопасен только вместе с `pool_pre_ping`: от молча убитого
соединения страхует пинг перед выдачей, а не возраст. Поэтому здесь два
сторожа, и второй важнее первого.

⚠ ДИВЕРСИИ (обе прогнаны, обе дали красный): вернуть `1800` в config.py —
краснеет первый; `"pool_pre_ping": False` в session.py — краснеет второй.
"""

from __future__ import annotations

from app.core.config import Settings, settings
from app.db import session as db_session

PG = "postgresql+asyncpg://u:p@h/db"


def test_умолчание_срока_два_часа() -> None:
    """Проверяется умолчание класса, а не `settings`: в прогоне значение может
    прийти из окружения. В бой уходит умолчание, только если боевой `.env` не
    задаёт `DB_POOL_RECYCLE_SECONDS` сам (в `.env.example` он задан: 1800) —
    это проверяется при выкатке, а не тестом."""
    assert Settings.model_fields["db_pool_recycle_seconds"].default == 7200


def test_долгий_срок_держится_на_pre_ping() -> None:
    kw = db_session._pool_kwargs(PG)
    assert kw["pool_pre_ping"] is True, (
        "без pre_ping двухчасовое соединение, убитое firewall'ом, роняет первый же запрос"
    )
    assert kw["pool_recycle"] == settings.db_pool_recycle_seconds
