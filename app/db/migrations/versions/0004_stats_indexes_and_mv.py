"""Sprint 4 — статистика: рабочие часы, индексы, materialized view.

Ровно то, что 06-STATS-REPORTS требует «сверх ядра схемы» (§0 преамбула):

1. SQL-функция ``business_seconds_between`` (06 §2.1) — рабочие часы
   10:00–20:00 Europe/Moscow, границы параметризованы (``work_start`` /
   ``work_end``), 7 дней в неделю: у Lead Partner нет выходных в сценарии
   бота (DESIGN §4.2). ``STRICT`` — критично: диалог без ответа даёт ``NULL``,
   и ``avg``/``percentile_cont`` его игнорируют, а не считают нулём.
2. Дополнительные индексы (06 §3.1) под FRT, «сообщений отправлено»,
   heatmap, snapshot-счётчики и все событийные метрики из ``audit_log``.
3. Materialized view ``mv_conversation_stats`` (06 §3.2) — пофактовая
   гранулярность (строка на диалог) + уникальный индекс, без которого
   невозможен ``REFRESH ... CONCURRENTLY`` (ежечасный job, 06 §3.2).

Почему обычный ``CREATE INDEX``, а не ``CONCURRENTLY``: на партиционированном
родителе ``messages`` CONCURRENTLY невозможен в принципе (06 §3.1), а внутри
транзакции Alembic — тем более. Ровно тот же выбор и по тем же причинам, что
в 0003; при разрастании ``messages`` индексы строятся руками по партициям
(``CONCURRENTLY`` + ``ALTER INDEX ... ATTACH PARTITION``) отдельным шагом
рантбука.

Что НЕ создаётся заново (уже есть в ядре, 06 §3.1 «не дублируем»):

* ``idx_messages_conv_created`` — это ``ix_messages_conversation_created``
  из 0002 (тот же ``(conversation_id, created_at)``);
* ``idx_conversations_last_message`` — ``ix_conversations_last_message_at`` (0002);
* ``idx_conversations_assignee_status`` — одноимённый ``ix_...`` (0002).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MV_NAME = "mv_conversation_stats"

# --- 06 §2.1 ----------------------------------------------------------------
BUSINESS_SECONDS_FN = """
CREATE OR REPLACE FUNCTION business_seconds_between(
    t0 timestamptz,
    t1 timestamptz,
    work_start interval DEFAULT interval '10 hours',
    work_end   interval DEFAULT interval '20 hours'
) RETURNS bigint
LANGUAGE sql STABLE STRICT AS $$
    SELECT COALESCE(sum(GREATEST(0, EXTRACT(epoch FROM
               LEAST   (t1 AT TIME ZONE 'Europe/Moscow', d + work_end)
             - GREATEST(t0 AT TIME ZONE 'Europe/Moscow', d + work_start)
           )))::bigint, 0)
    FROM generate_series(
           date_trunc('day', t0 AT TIME ZONE 'Europe/Moscow'),
           date_trunc('day', t1 AT TIME ZONE 'Europe/Moscow'),
           interval '1 day') AS d
$$
"""

# --- 06 §3.1 ----------------------------------------------------------------
INDEXES: tuple[tuple[str, str], ...] = (
    # «Сообщений отправлено по менеджеру» (06 §2.6, §2.8, виджет §6):
    # частичный — исходящие операторов это ~10–20% строк.
    (
        "idx_messages_operator_out",
        "CREATE INDEX IF NOT EXISTS idx_messages_operator_out "
        "ON messages (sender_user_id, created_at) "
        "WHERE direction = 'out' AND sender_type = 'operator'",
    ),
    # Входящие клиентов: тепловая карта (06 §3.4) и таймсерия messages_in.
    (
        "idx_messages_client_in",
        "CREATE INDEX IF NOT EXISTS idx_messages_client_in "
        "ON messages (created_at) "
        "WHERE direction = 'in' AND sender_type = 'client'",
    ),
    # Snapshot «в работе / ждут ответа сейчас» с фильтром по аккаунту (06 §2.3).
    (
        "idx_conversations_account_status",
        "CREATE INDEX IF NOT EXISTS idx_conversations_account_status "
        "ON conversations (account_id, status)",
    ),
    # «Повторные клиенты» (06 §2.7б): EXISTS по прошлым диалогам клиента.
    (
        "idx_conversations_client",
        "CREATE INDEX IF NOT EXISTS idx_conversations_client ON conversations (client_id)",
    ),
    # Все событийные метрики (06 §2.3–2.5, §2.7, §2.8, виджет §6).
    (
        "idx_audit_action_created",
        "CREATE INDEX IF NOT EXISTS idx_audit_action_created ON audit_log (action, created_at)",
    ),
    # Лента аудита по сущности + LATERAL «последнее закрытие» в MV.
    (
        "idx_audit_entity",
        "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity, entity_id, created_at)",
    ),
)

# --- 06 §3.2 ----------------------------------------------------------------
MV_DDL = f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS {MV_NAME} AS
WITH base AS (
    SELECT c.id, c.account_id, c.client_id, c.assignee_id, c.status,
           f.first_client_at
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    WHERE f.first_client_at IS NOT NULL
)
SELECT b.id                                       AS conversation_id,
       b.account_id, b.client_id, b.assignee_id, b.status,
       b.first_client_at,
       op.first_operator_at,
       op.first_operator_user_id,
       bt.first_bot_at,
       EXTRACT(epoch FROM op.first_operator_at - b.first_client_at)::int
                                                  AS frt_operator_sec,
       business_seconds_between(b.first_client_at, op.first_operator_at)
                                                  AS frt_operator_biz_sec,
       EXTRACT(epoch FROM bt.first_bot_at - b.first_client_at)::int
                                                  AS frt_bot_sec,
       msg.msgs_in, msg.msgs_out_operator, msg.msgs_out_bot,
       (msg.msgs_out_operator > 0)                AS has_operator_reply,
       cl.closed_at, cl.closed_by
FROM base b
LEFT JOIN LATERAL (
    SELECT m.created_at     AS first_operator_at,
           m.sender_user_id AS first_operator_user_id
    FROM messages m
    WHERE m.conversation_id = b.id
      AND m.created_at >= b.first_client_at
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.delivery_status <> 'failed'
    ORDER BY m.created_at
    LIMIT 1
) op ON true
LEFT JOIN LATERAL (
    SELECT min(m.created_at) AS first_bot_at
    FROM messages m
    WHERE m.conversation_id = b.id
      AND m.created_at >= b.first_client_at
      AND m.direction = 'out' AND m.sender_type = 'bot'
      AND m.delivery_status <> 'failed'
) bt ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE m.direction = 'in'
                              AND m.sender_type = 'client')   AS msgs_in,
           count(*) FILTER (WHERE m.direction = 'out'
                              AND m.sender_type = 'operator'
                              AND m.delivery_status <> 'failed') AS msgs_out_operator,
           count(*) FILTER (WHERE m.direction = 'out'
                              AND m.sender_type = 'bot'
                              AND m.delivery_status <> 'failed') AS msgs_out_bot
    FROM messages m
    WHERE m.conversation_id = b.id
) msg ON true
LEFT JOIN LATERAL (
    SELECT a.created_at     AS closed_at,
           a.details->>'by' AS closed_by
    FROM audit_log a
    WHERE a.entity = 'conversation'
      AND a.entity_id = b.id::text
      AND a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
    ORDER BY a.created_at DESC
    LIMIT 1
) cl ON true
"""

MV_INDEXES: tuple[str, ...] = (
    # Уникальный индекс обязателен для REFRESH MATERIALIZED VIEW CONCURRENTLY.
    f"CREATE UNIQUE INDEX IF NOT EXISTS {MV_NAME}_pk ON {MV_NAME} (conversation_id)",
    f"CREATE INDEX IF NOT EXISTS {MV_NAME}_first_client_at ON {MV_NAME} (first_client_at)",
    f"CREATE INDEX IF NOT EXISTS {MV_NAME}_account ON {MV_NAME} (account_id, first_client_at)",
    f"CREATE INDEX IF NOT EXISTS {MV_NAME}_operator "
    f"ON {MV_NAME} (first_operator_user_id, first_client_at)",
    f"CREATE INDEX IF NOT EXISTS {MV_NAME}_closed_at ON {MV_NAME} (closed_at)",
)


def upgrade() -> None:
    # Порядок обязателен: MV вызывает функцию в своём теле, а её LATERAL по
    # audit_log опирается на idx_audit_entity.
    op.execute(BUSINESS_SECONDS_FN)
    for _name, ddl in INDEXES:
        op.execute(ddl)
    op.execute(MV_DDL)
    for ddl in MV_INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    # Индексы MV уходят вместе с ней; функция — только после MV (зависимость).
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME}")
    for name, _ddl in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(
        "DROP FUNCTION IF EXISTS business_seconds_between(timestamptz, timestamptz, "
        "interval, interval)"
    )
