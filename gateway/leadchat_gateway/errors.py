"""Единая форма ответа шлюза.

Успех — HTTP 200 и `{"ok": true, ...}`. Отказ провайдера — тоже HTTP 200, но
`{"ok": false, "kind": ..., "status": <код провайдера или null>, "detail": ...}`:
так LeadChat отличает «шлюз жив, провайдер отказал» от «шлюза нет» (обрыв,
5xx, 401 самого шлюза).

`kind` (как у `GeocodeError` в LeadChat, плюс два своих):
- `network` — таймаут/обрыв/5xx/429 провайдера, повтор уместен;
- `blocked` — 401/403 или HTML вместо JSON, повторять вредно (политика OSM);
- `bad_response` — JSON не той формы;
- `no_key` — ключа в окружении шлюза нет: LeadChat помечает провайдера
  «без ключа» и больше не спрашивает до обновления снимка `/status`;
- `exhausted` — все точки/модели читателя исчерпаны;
- `auth` — провайдер отверг ключ (только там, где 401 отличим от 403).

В `detail` — короткая причина без адреса, тела и ключа: ответ идёт в лог и
на экран монитора LeadChat.
"""

from __future__ import annotations

from typing import Any

KINDS = frozenset({"network", "blocked", "bad_response", "no_key", "exhausted", "auth"})


class ProviderError(Exception):
    """Провайдер не ответил по-человечески. Без URL и тела — в них адрес."""

    def __init__(self, kind: str, status: int | None = None, detail: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown kind: {kind}")
        self.kind = kind
        self.status = status
        self.detail = detail
        super().__init__(f"{kind}" + (f" ({status})" if status else ""))


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def fail(kind: str, status: int | None = None, detail: str = "", **extra: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return {"ok": False, "kind": kind, "status": status, "detail": detail, **extra}


def from_error(exc: ProviderError, **extra: Any) -> dict[str, Any]:
    return fail(exc.kind, exc.status, exc.detail, **extra)
