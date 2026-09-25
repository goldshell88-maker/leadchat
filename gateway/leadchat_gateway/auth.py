"""Сервисный токен LeadChat.

Дверь и так только внутрь WireGuard (`bind 10.10.0.2`, единственный пир —
прод LeadChat), токен — второй замок: чужой процесс на любой из двух машин
не должен тратить платные ключи. Сравнение — `hmac.compare_digest`, чтобы
по времени ответа нельзя было подобрать токен.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from leadchat_gateway.config import settings


def require_token(authorization: str = Header(default="")) -> None:
    ожидаемый = settings.gateway_token.strip()
    if not ожидаемый:
        # Пустой токен = шлюз не настроен. 503, а не «пускаем всех».
        raise HTTPException(status_code=503, detail="GATEWAY_TOKEN не задан")
    схема, _, токен = authorization.partition(" ")
    # Байтами: `compare_digest` на строках с не-ASCII падает TypeError'ом, и
    # кривой заголовок давал бы 500 вместо 401.
    if схема.lower() != "bearer" or not hmac.compare_digest(
        токен.strip().encode("utf-8"), ожидаемый.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="токен не принят")
