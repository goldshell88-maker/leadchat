"""Спринт 6 — движок ботов: метки времени, индексы привязки, дефолтный бот.

Таблица ``bots`` и внешний ключ ``avito_accounts.bot_id`` уже есть в ядре
схемы (DESIGN §4.4 -> миграция 0001) — здесь **только то, чего там нет**:

1. ``bots.created_at`` / ``bots.updated_at``. В DDL DESIGN §4.4 их нет, а
   редактор (02 §5.1) и список ботов (01 §8.1) показывают «когда изменён», и
   `audit_log: bot.updated` (06 §0.3) должен сходиться со строкой. Колонки
   ``NOT NULL DEFAULT now()`` — существующие строки получают now(), переписи
   таблицы нет (для timestamptz с volatile-дефолтом PostgreSQL ≥ 11 пишет
   значение при обновлении строки, а не переписывает всю таблицу).
2. Индекс на ``avito_accounts.bot_id``. FK без индекса — классическая
   грабля: его читают ``GET /bots`` (бейджи аккаунтов, 01 §8.1),
   ``PUT /bots/{id}/accounts`` (§8.5), проверка ``bot_in_use`` перед
   удалением (§8.7) и **каждый тик бота** (`get_bot_for_conversation`), а
   ещё он нужен PostgreSQL при удалении строки ``bots``, иначе это
   seq scan под блокировкой.
3. Частичный индекс «диалоги, которыми сейчас владеет бот» — под рантбук
   05 §8 («кто у бота завис») и будущий фильтр списка; частичный, потому что
   ``bot_active`` истинно у единиц процентов строк.
4. Сид дефолтного бота «Первичный приём» (02 §1.5) — при первом запуске, если
   ботов ещё нет вовсе.

Про сид отдельно. Он **выключен** (``is_enabled = false``) и ни к одному
аккаунту не привязан: включение бота — осознанное действие админа, который
сначала заполнит базу знаний (пустая ``knowledge_base`` + шаг ``ai_answer``
даёт предупреждение валидатора ``ai_without_kb``, 02 §5.2) и выберет
аккаунты. Сценарий вшит в миграцию копией, а не читается из
``app/bots/scenarios/primary_intake.json``: миграция обязана давать один и тот
же результат через год, когда файл в коде уже поменяется.

Отличие сценария от примера 02 §1.5 ровно одно и повторяет решение владельца:
ветка таймаута ``ask_problem`` ведёт не на ``close``, а на handoff —
автоматического закрытия диалогов по таймауту в продукте НЕТ, диалог живёт,
пока менеджер не закроет его руками.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""

import json
from collections.abc import Sequence
from typing import Any

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACCOUNTS_BOT_INDEX = "ix_avito_accounts_bot_id"
CONVERSATIONS_BOT_INDEX = "ix_conversations_bot_active"

# Фиксированный id, чтобы downgrade снял ровно сидовую строку и не тронул
# ботов, созданных людьми.
DEFAULT_BOT_ID = "b07f0001-0000-4000-8000-000000000001"
DEFAULT_BOT_NAME = "Первичный приём"

DEFAULT_SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "greet",
    "settings": {"max_bot_messages_row": 5, "max_offscript_messages": 2},
    "steps": [
        {
            "id": "greet",
            "type": "send",
            "params": {
                "text": (
                    "Здравствуйте, {client_name}! Это сервис Lead Partner 👋\n"
                    "Подскажите, что случилось с техникой — модель и проблему?"
                )
            },
            "next": "ask_problem",
        },
        {
            "id": "ask_problem",
            "type": "ask",
            "params": {
                "text": None,
                "var": "problem",
                "validate": "any",
                "retry_text": None,
                "max_attempts": 1,
                "timeout": "24h",
            },
            "next": "ai_draft",
            "on_timeout": "handoff_no_reply",
            "on_invalid": None,
        },
        {
            "id": "ai_draft",
            "type": "ai_answer",
            "params": {
                "confidence_threshold": 0.6,
                "max_reply_len": 800,
                "context_messages": 10,
            },
            "next": "check_hours",
            "on_low_confidence": None,
        },
        {
            "id": "check_hours",
            "type": "condition",
            "params": {
                "conditions": [
                    {
                        "if": {
                            "kind": "work_hours",
                            "from": "10:00",
                            "to": "20:00",
                            "timezone": "Europe/Moscow",
                        },
                        "next": "handoff_day",
                    }
                ],
                "else": "night_msg",
            },
        },
        {
            "id": "handoff_day",
            "type": "handoff",
            "params": {
                "reason": "scenario",
                "comment": "Клиент описал проблему, бот дал предварительный ответ",
                "tags": ["первичный-приём"],
            },
        },
        {
            "id": "night_msg",
            "type": "send",
            "params": {"text": "Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔"},
            "next": "ask_phone",
        },
        {
            "id": "ask_phone",
            "type": "ask",
            "params": {
                "text": None,
                "var": "phone",
                "validate": "phone",
                "retry_text": (
                    "Кажется, это не номер телефона 🙂 Напишите в формате +7 900 000-00-00"
                ),
                "max_attempts": 2,
                "timeout": "12h",
            },
            "next": "tag_contact",
            "on_timeout": "handoff_night",
            "on_invalid": "handoff_night",
        },
        {
            "id": "tag_contact",
            "type": "tag",
            "params": {"tags": ["контакт собран"]},
            "next": "note_contact",
        },
        {
            "id": "note_contact",
            "type": "note",
            "params": {
                "text": "🤖 Бот собрал контакт: {phone}\nПроблема со слов клиента: {problem}"
            },
            "next": "handoff_night",
        },
        {
            "id": "handoff_night",
            "type": "handoff",
            "params": {
                "reason": "scenario",
                "comment": "Ночной диалог: проблема зафиксирована, перезвонить утром первыми",
                "tags": ["ночной-лид"],
            },
        },
        {
            "id": "handoff_no_reply",
            "type": "handoff",
            "params": {
                "reason": "scenario",
                "comment": "Клиент не ответил на первый вопрос — диалог возвращён в общую очередь",
                "tags": [],
            },
        },
    ],
}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {ACCOUNTS_BOT_INDEX} "
        f"ON avito_accounts (bot_id) WHERE bot_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {CONVERSATIONS_BOT_INDEX} "
        f"ON conversations (updated_at) WHERE bot_active"
    )

    # Дефолтный бот — только на пустой таблице (первый запуск).
    op.execute(
        f"""
        INSERT INTO bots (id, name, is_enabled, schedule, scenario, knowledge_base)
        SELECT '{DEFAULT_BOT_ID}'::uuid,
               '{DEFAULT_BOT_NAME}',
               false,
               '{{"always": true}}'::jsonb,
               '{json.dumps(DEFAULT_SCENARIO, ensure_ascii=False).replace("'", "''")}'::jsonb,
               NULL
        WHERE NOT EXISTS (SELECT 1 FROM bots)
        """
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.execute(f"UPDATE avito_accounts SET bot_id = NULL WHERE bot_id = '{DEFAULT_BOT_ID}'::uuid")
    op.execute(f"DELETE FROM bots WHERE id = '{DEFAULT_BOT_ID}'::uuid")
    op.execute(f"DROP INDEX IF EXISTS {CONVERSATIONS_BOT_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {ACCOUNTS_BOT_INDEX}")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS created_at")
