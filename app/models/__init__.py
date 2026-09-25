"""All ORM models. Importing this package registers every table on Base.metadata
(required by Alembic autogenerate and tests' create_all).

ЗДЕСЬ ОБЯЗАНЫ БЫТЬ ВСЕ МОДЕЛИ ДО ЕДИНОЙ, И ЦЕНА ПРОПУСКА — УДАЛЁННАЯ ТАБЛИЦА.

`notifications` и `notification_reads` не были перечислены здесь с самой их
постановки (миграция 0006). Работало всё: центр уведомлений импортирует
`app.models.notification` напрямую, и таблицы у него на месте. Не работало
одно — автогенерация миграции: `alembic revision --autogenerate` сравнивает
базу с `Base.metadata`, а туда попадает только импортированное ЭТИМ модулем.
Незарегистрированная таблица выглядит для него лишней, и в новую миграцию
уезжает `op.drop_table("notifications")`. Ревьюер такую строку в чужой
миграции про соседнюю задачу пропускает легко, а на проде это стирание всего
центра уведомлений вместе с историей поломок за 90 дней.

Поэтому новая модель добавляется сюда В ТОТ ЖЕ КОММИТ, что и сам файл. Что
список полон, проверяет `tests/unit/test_models_registry.py`: он поднимает
отдельный процесс, импортирует пакет, затем каждый модуль `app/models/*.py`
по отдельности и требует, чтобы второй шаг не добавил в метаданные ни одной
новой таблицы.
"""

from app.models.account import AvitoAccount
from app.models.account_operator import AccountOperator
from app.models.address_funnel import AddressFunnelWeek
from app.models.api_registry import ApiRegistryEntry
from app.models.app_setting import AppSetting
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.bot import Bot
from app.models.client import Client, ClientAddressCandidate, ClientMergeVeto, ClientPhoneCandidate
from app.models.conversation import Conversation
from app.models.conversation_decline import ConversationDecline
from app.models.conversation_participant import ConversationParticipant
from app.models.conversation_pin import ConversationPin
from app.models.lead import LeadHandout
from app.models.leadbot import LeadbotCall
from app.models.message import Message
from app.models.message_idempotency import MessageIdempotency
from app.models.notification import Notification, NotificationRead
from app.models.template import Template
from app.models.user import User
from app.models.webhook_raw import WebhookRawLog

__all__ = [
    "AccountOperator",
    "AddressFunnelWeek",
    "ApiRegistryEntry",
    "AuditLog",
    "AppSetting",
    "AvitoAccount",
    "Base",
    "Bot",
    "Client",
    "ClientAddressCandidate",
    "ClientMergeVeto",
    "ClientPhoneCandidate",
    "Conversation",
    "ConversationDecline",
    "ConversationParticipant",
    "ConversationPin",
    "LeadHandout",
    "LeadbotCall",
    "Message",
    "MessageIdempotency",
    "Notification",
    "NotificationRead",
    "Template",
    "User",
    "WebhookRawLog",
]
