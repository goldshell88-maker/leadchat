"""Shared test setup.

Settings validate at import (08 §1.3), so every required env var must be
present BEFORE any ``app.*`` import — this conftest is imported by pytest
first, which guarantees the ordering.
"""

import base64
import os

os.environ.setdefault("ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-prod-0123456789abcdef")
os.environ.setdefault("TOKEN_ENC_KEY", base64.b64encode(b"\x01" * 32).decode())
os.environ.setdefault("AVITO_CLIENT_ID", "test-client-id")
os.environ.setdefault("AVITO_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("MEDIA_SIGN_KEY", "test-media-sign-key")
# AI-подсистема в тестах — только заглушка (02 §3, 07 §1.1): ни один тест не
# имеет права пойти в сеть. Тесты самого app/bots/ai.py переопределяют флаг
# точечно и подменяют клиент фейком или respx на адрес шлюза (docs/46) —
# ключ Anthropic живёт на шлюзе, в окружении LeadChat его больше нет.
os.environ.setdefault("AI_FAKE", "1")
os.environ.setdefault("APP_VERSION", "test-version")
# Пустые значения → дефолтная деривация от https://{DOMAIN}. Env-переменные
# приоритетнее .env-файла — иначе локальный dev-.env (PUBLIC_BASE_URL=
# http://host.docker.internal:8000) ломает ассерты на дефолтные URL.
os.environ.setdefault("PUBLIC_BASE_URL", "")
os.environ.setdefault("AVITO_REDIRECT_URI", "")
# ЗАХВАТ ЛОГОВ ДОЛЖЕН РАБОТАТЬ НЕЗАВИСИМО ОТ ПОРЯДКА ТЕСТОВ.
#
# В бою structlog запоминает цепочку обработчиков на логгере (кэш экономит на
# каждой строке). Логгер модуля запоминает её при ПЕРВОМ использовании — и если
# он успел поработать в обычном тесте раньше, `structlog.testing.capture_logs()`
# до него уже не достучится: строки уйдут в stdout по запомненной цепочке, а
# захват вернёт пустой список. Тест прочитает это как «в журнале ни строчки».
#
# Найдено 12 августа на первом прогоне ВСЕГО набора одним процессом:
# интеграционные тесты идут раньше модульных, поднимают приложение вместе с
# боевой настройкой логов — и через пять минут падает тест, который в одиночку
# и своим файлом зелёный. Тестов с захватом логов четыре, и лотереей был каждый;
# сегодня просто выпало одному.
#
# Ставится ДО импорта `app.*` — вместе с остальными переменными окружения,
# потому что настройка читается в момент вызова `configure_logging`, а зовёт
# её приложение при старте, уже внутри прогона.
os.environ.setdefault("LOG_CACHE", "0")
