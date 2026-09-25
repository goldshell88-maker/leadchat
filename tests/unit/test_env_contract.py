"""Имена переменных окружения — контракт 05 §4.

Настройка, о которой знает только `app/core/config.py`, для эксплуатации не
существует: её не задать на деплое и не найти в разборе инцидента. Поэтому
каждое поле `Settings` обязано быть названо хотя бы в одном из примеров
окружения — dev или прод.
"""

import pathlib

import pytest

from app.core.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEV_ENV = ROOT / ".env.example"
PROD_ENV = ROOT / ".env.prod.example"

# Осознанные исключения (пусто). Строка здесь = решение «эту настройку в
# контракт не выносим», а не забытое поле.
UNDOCUMENTED_ON_PURPOSE: frozenset[str] = frozenset()


def _documented() -> str:
    return DEV_ENV.read_text(encoding="utf-8") + PROD_ENV.read_text(encoding="utf-8")


def test_env_examples_exist():
    assert DEV_ENV.is_file() and PROD_ENV.is_file()


@pytest.mark.parametrize("field", sorted(Settings.model_fields))
def test_every_setting_is_named_in_an_env_example(field: str):
    if field in UNDOCUMENTED_ON_PURPOSE:
        pytest.skip("исключено сознательно")
    assert f"{field.upper()}=" in _documented(), (
        f"{field.upper()} не задокументирована ни в .env.example, ни в .env.prod.example — "
        "имена env объявлены контрактом 05 §4"
    )


@pytest.mark.parametrize(
    "name",
    [
        # Долг спринта 4: пул и его страховки от «idle in transaction» (05 §8).
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        "DB_STATEMENT_TIMEOUT_MS",
        "READ_MARKER_TTL_DAYS",
        "HEALTH_QUEUE_LEN_RED",
        "HEALTH_OLDEST_PENDING_SEC_RED",
        "HEALTH_FAILED_LAST_HOUR_RED",
        "HEALTH_DEEP_CHECK_SCHEDULER",
        "SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS",
        "SMOKE_USER_EMAIL",
        "SMOKE_CONVERSATION_EXTERNAL_ID",
        "SMOKE_ACCOUNT_TITLE",
        "SMOKE_AVITO_USER_ID",
    ],
)
def test_production_env_documents_the_sprint4_variables(name: str):
    assert f"{name}=" in PROD_ENV.read_text(encoding="utf-8")


def test_production_enables_the_deep_scheduler_check():
    """05 §7.2: без этого мёртвый планировщик не попадёт в общий status."""
    assert "HEALTH_DEEP_CHECK_SCHEDULER=true" in PROD_ENV.read_text(encoding="utf-8")
