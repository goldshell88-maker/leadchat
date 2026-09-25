"""Настройки шлюза: окружение из `/opt/leadchat-gateway/.env` (systemd
`EnvironmentFile`) или из переменных процесса.

Имена ключей провайдеров — те же, что раньше стояли в `.env` прода LeadChat
(`app/core/config.py` до 16.09): владелец переносит строки как есть. Ни одно
значение никогда не печатается и не логируется.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- сам шлюз ---
    #: Сервисный токен LeadChat (`GATEWAY_TOKEN` в .env прода). Пустой — шлюз
    #: отвечает 503 всем: пускать по пустому токену хуже, чем не работать.
    gateway_token: str = ""
    #: Куда слушать. Только адрес внутри WireGuard — из интернета шлюза нет.
    gateway_host: str = "10.10.0.2"
    gateway_port: int = 8792

    # --- карты и справочники ---
    nominatim_contact: str = ""  # e-mail в User-Agent, как просит политика OSM
    yandex_geocoder_key: str = ""
    yandex_suggest_key: str = ""
    dadata_api_key: str = ""

    # --- читатели адреса: OpenRouter → Groq → Mistral ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_address_models: str = ""  # CSV; пусто — умолчания провайдера
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_address_models: str = ""
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_address_models: str = ""

    # --- Anthropic (ответы ботов LeadChat) ---
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    @property
    def listen(self) -> str:
        return f"{self.gateway_host}:{self.gateway_port}"


settings = Settings()
