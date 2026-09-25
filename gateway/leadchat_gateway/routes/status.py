"""GET /status — у кого из провайдеров есть ключ (без самих ключей).

LeadChat держит этот снимок в процессе (`gateway.known_keys`) и по нему решает
`enabled()` провайдеров; монитор показывает «ключ на шлюзе не задан».
"""

from __future__ import annotations

from fastapi import APIRouter

from leadchat_gateway.config import settings

router = APIRouter()

#: Провайдер → (нужен ли ключ, поле настройки с ключом, поле/значение адреса).
PROVIDERS: tuple[tuple[str, str | None, str], ...] = (
    (
        "dadata",
        "dadata_api_key",
        "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address",
    ),
    ("nominatim", None, "https://nominatim.openstreetmap.org/search"),
    ("yandex_geocoder", "yandex_geocoder_key", "https://geocode-maps.yandex.ru/1.x/"),
    ("yandex_suggest", "yandex_suggest_key", "https://suggest-maps.yandex.ru/v1/suggest"),
    ("ahunter", None, "https://ahunter.ru/site/suggest/address"),
    ("speller", None, "https://speller.yandex.net/services/spellservice.json/checkText"),
    ("openrouter", "openrouter_api_key", "openrouter_base_url"),
    ("groq", "groq_api_key", "groq_base_url"),
    ("mistral", "mistral_api_key", "mistral_base_url"),
    ("anthropic", "anthropic_api_key", "anthropic_base_url"),
)

#: Пустая строка `*_BASE_URL=` в .env не должна ломать пробы: работа идёт на
#: адрес по умолчанию, значит и проба — туда же.
_УМОЛЧАНИЯ = {
    "openrouter_base_url": "https://openrouter.ai/api/v1",
    "groq_base_url": "https://api.groq.com/openai/v1",
    "mistral_base_url": "https://api.mistral.ai/v1",
    "anthropic_base_url": "https://api.anthropic.com",
}


def key_present(provider: str) -> bool | None:
    """True/False — ключ есть/нет; None — ключ не нужен."""
    for имя, поле, _ in PROVIDERS:
        if имя == provider:
            if поле is None:
                return None
            return bool((getattr(settings, поле, "") or "").strip())
    raise KeyError(provider)


def base_url(provider: str) -> str:
    for имя, _, адрес in PROVIDERS:
        if имя == provider:
            if адрес.startswith("http"):
                return адрес
            значение = (getattr(settings, адрес, "") or "").strip() or _УМОЛЧАНИЯ[адрес]
            return значение.rstrip("/")
    raise KeyError(provider)


def snapshot() -> dict[str, dict[str, object]]:
    return {
        имя: {"key_present": key_present(имя), "base_url": base_url(имя).split("?", 1)[0]}
        for имя, _, _ in PROVIDERS
    }


@router.get("/status")
async def status() -> dict[str, object]:
    return {"ok": True, "providers": snapshot()}
