"""Сторож решения владельца 16.09 (docs/46): в LeadChat нет кода походов к
внешним API и нет их ключей — всё это на шлюзе Амстердама.

Три проверки по тексту исходников, а не по поведению: дефект «кто-то завёл
прямой поход к DaData в обход шлюза» тестами поведения не ловится — он
просто работает, пока Амстердам жив."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: Адреса API провайдеров. Ссылки на ДОКУМЕНТАЦИЮ (dadata.ru/api, console.groq.com)
#: разрешены — это подсказки человеку в мониторе, не походы.
ХОСТЫ_ПРОВАЙДЕРОВ = (
    "suggestions.dadata.ru",
    "nominatim.openstreetmap.org",
    "geocode-maps.yandex.ru",
    "suggest-maps.yandex.ru",
    "speller.yandex.net",
    "ahunter.ru/site/suggest/address",
    "openrouter.ai/api",
    "api.groq.com",
    "api.mistral.ai",
    "api.anthropic.com",
)

#: Поля, которых в `Settings` больше нет и быть не должно.
КЛЮЧИ_ПРОВАЙДЕРОВ = (
    "dadata_api_key",
    "dadata_secret_key",
    "yandex_geocoder_key",
    "yandex_suggest_key",
    "nominatim_contact",
    "openrouter_api_key",
    "groq_api_key",
    "mistral_api_key",
    "anthropic_api_key",
    "anthropic_base_url",
)

#: Где в `app/` законно создаётся `httpx.AsyncClient`: канал Авито, лид-бот,
#: сам клиент шлюза, прямая проба `/health` шлюза в мониторе и всё, что не
#: ходит к провайдерам адреса/моделей. Новый файл в списке — повод спросить,
#: не обход ли это шлюза.
РАЗРЕШЁННЫЕ_HTTPX = {
    "app/integrations/gateway.py",
    "app/integrations/avito/client.py",
    "app/bots/leadbot.py",
    "app/services/api_monitor.py",
    "app/workers/inbound.py",  # канарейка вебхука — поход к самому себе
    "app/workers/transcribe.py",  # скачивание голосовых Авито для локального Whisper
}


def _исходники() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_в_app_нет_адресов_провайдеров() -> None:
    нарушители = []
    for файл in _исходники():
        текст = файл.read_text(encoding="utf-8")
        for хост in ХОСТЫ_ПРОВАЙДЕРОВ:
            if хост in текст:
                нарушители.append(f"{файл.relative_to(ROOT)}: {хост}")
    assert нарушители == [], "походы к провайдерам живут на шлюзе (docs/46):\n" + "\n".join(
        нарушители
    )


def test_в_настройках_нет_ключей_провайдеров() -> None:
    лишние = [к for к in КЛЮЧИ_ПРОВАЙДЕРОВ if к in Settings.model_fields]
    assert лишние == [], f"ключи провайдеров живут только на шлюзе: {лишние}"
    assert {"gateway_url", "gateway_token"} <= set(Settings.model_fields)


def test_httpx_клиенты_только_в_разрешённых_местах() -> None:
    образец = re.compile(r"httpx\.AsyncClient\(")
    где = sorted(
        str(файл.relative_to(ROOT))
        for файл in _исходники()
        if образец.search(файл.read_text(encoding="utf-8"))
    )
    лишние = sorted(set(где) - РАЗРЕШЁННЫЕ_HTTPX)
    assert лишние == [], f"новый HTTP-клиент вне шлюза — проверьте, не обход ли: {лишние}"
