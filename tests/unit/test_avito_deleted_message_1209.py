"""«Сообщение удалено» — серая запись Авито, а не реплика клиента (12.09).

Авито не объявляет удалённое сообщение видом: в 30 днях сырца нет ни одного
`type: deleted`, зато 496 записей пришли обычным текстом «Сообщение удалено»
и ложились репликой клиента — пузырь, непрочитанное, ожидание ответа
(владелец: «выглядит как текст»). Узнаём по слову — единственный признак,
который Авито оставляет, — на обеих дорогах: живой вебхук и история.
"""

from __future__ import annotations

from typing import Any

from app.integrations.avito import adapter as ad
from app.integrations.avito.adapter import AvitoAdapter, ChatInfo

ACCOUNT_UID = 770200
CLIENT_UID = 999201


def _webhook(*, text: str, author: int) -> dict[str, Any]:
    return {
        "id": "env-1",
        "version": "v3.0.0",
        "timestamp": 1757600000,
        "payload": {
            "type": "message",
            "value": {
                "id": "am-1",
                "chat_id": "u2i-1",
                "user_id": ACCOUNT_UID,
                "author_id": author,
                "created": 1757600000,
                "type": "text",
                "content": {"text": text},
            },
        },
    }


def _chat() -> ChatInfo:
    return ChatInfo(
        external_chat_id="u2i-1",
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
        item_title="Ремонт",
        item_url=None,
        item_price=None,
        has_unread=False,
        unread_count=None,
        last_message_at=None,
    )


def test_живой_вебхук_удалённое_клиентом_это_серая_запись() -> None:
    event = AvitoAdapter.parse_webhook(_webhook(text=ad.AVITO_DELETED_TEXT, author=CLIENT_UID))
    assert event.is_system is True
    assert event.source_type == "deleted"
    assert event.text == ad.DELETED_BY_CLIENT
    assert event.attachments == []


def test_живой_вебхук_удалённое_сотрудником() -> None:
    event = AvitoAdapter.parse_webhook(_webhook(text=ad.AVITO_DELETED_TEXT, author=ACCOUNT_UID))
    assert event.is_system is True and event.text == ad.DELETED_BY_STAFF


def test_история_удалённое_это_серая_запись() -> None:
    event = AvitoAdapter.normalize_history_message(
        {
            "id": "h-1",
            "author_id": CLIENT_UID,
            "created": 1757600000,
            "type": "text",
            "content": {"text": "  Сообщение удалено "},
        },
        chat=_chat(),
        account_user_id=ACCOUNT_UID,
    )
    assert event.is_system is True and event.text == ad.DELETED_BY_CLIENT


def test_обычный_текст_и_текст_с_вложением_не_трогаются() -> None:
    event = AvitoAdapter.parse_webhook(_webhook(text="Сообщение удалено?", author=CLIENT_UID))
    assert event.is_system is False and event.text == "Сообщение удалено?"
    raw = _webhook(text=ad.AVITO_DELETED_TEXT, author=CLIENT_UID)
    raw["payload"]["value"]["content"]["image"] = {
        "id": "img-1",
        "sizes": {"1280x960": "https://40.img.avito.st/image/1/x.jpg"},
    }
    event = AvitoAdapter.parse_webhook(raw)
    assert event.is_system is False and event.attachments


def test_миграция_переносит_накопленное() -> None:
    import pathlib

    src = pathlib.Path("app/db/migrations/versions/0075_deleted_messages.py").read_text()
    assert "Клиент удалил сообщение" in src and "sender_type = 'avito'" in src
