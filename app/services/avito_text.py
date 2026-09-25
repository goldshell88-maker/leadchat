"""Нарезка текста ответа под лимит мессенджера Авито — одна на доставку и приём.

Доставка режет длинный ответ на части (`workers/deliver.py`), а приём эха
узнаёт своё по тексту части (`inbound._apply_external_outgoing`). Правило
нарезки обязано быть одним: разойдись они в границе части на символ — и эхо
первой части снова легло бы в ленту чужим сообщением (проверка 24.09).
"""

from __future__ import annotations

AVITO_TEXT_LIMIT = 1000  # лимит мессенджера Авито на одно сообщение (01 §6.2)


def split_text(text: str, limit: int = AVITO_TEXT_LIMIT) -> list[str]:
    """Режем длинный текст по границе абзаца/слова, не по середине слова."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest.strip())
            break
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit  # одно слово длиннее лимита — режем жёстко
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [p for p in parts if p]
