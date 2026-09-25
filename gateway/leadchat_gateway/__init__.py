"""Шлюз внешних API LeadChat (docs/46-ШЛЮЗ-API.md).

Живёт на Амстердаме рядом с лид-ботом, слушает только адрес внутри WireGuard и
держит у себя ключи и код походов к DaData, OSM, Яндексу, Ahunter, Спеллеру,
читателям адреса (OpenRouter → Groq → Mistral) и Anthropic. LeadChat знает один
адрес и один токен; шлюз лёг — все помощники молчат (требование владельца).

Без базы и Redis: кэш, счётчики, кулдауны и настройки — у LeadChat. Пакет не
зависит от `app.*` и обязан работать и на Python 3.12 (тесты в корне LeadChat),
и на 3.14 (системный python Амстердама).
"""

from __future__ import annotations

from pathlib import Path

#: Версия — короткий хеш коммита, который `deploy.sh` кладёт в файл VERSION рядом
#: с пакетом; в тестах и при запуске из дерева файла нет.
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"
