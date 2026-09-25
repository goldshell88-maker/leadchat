"""Индексы: один нужный завести, пять мёртвых снять.

⚠ ЧИСЛА ИЗ БОЯ 03.09 (pg_stat_user_indexes, окно накопления не меньше 20 суток).

ЗАВОДИМ. `sender_type = 'bot'` — 0,196 % строк `messages` (642 из 327 358), и
индекса по нему НЕТ. Каждое обращение к признаку «бот трогал этот диалог»
превращается в перебор секционированной таблицы, и экран «Диалоги бота» платит
за это ДЕВЯТЬ раз за открытие: семь счётчиков плюс `COUNT` и выборка страницы в
`query_table`. Замер: 452,9 мс и 129 783 буфера (около 1 ГБ логических чтений)
ради 642 строк — с вымыванием общего кеша у всех тринадцати диспетчеров.

Приём в проекте уже узаконен: ровно так сделаны `idx_messages_client_in` и
`idx_messages_operator_out` в 0004. Индекс объявляется на РОДИТЕЛЕ — PostgreSQL
разложит его по всем секциям сам, включая будущие.

СНИМАЕМ. Пять индексов с нулём сканов, у которых проверен КОД, а не только
счётчик: индекс может быть нужен редкому экрану, и счётчик этого не покажет.
Каждый из пяти проверен грепом по колонке в WHERE/ORDER BY.

⚠ ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ, хотя сканов у них тоже ноль:
* `ix_conversations_item_city` (1816 кБ) — дорога под фильтр «Город» (docs/32
  §5), который ещё не выкачен. Это вопрос владельцу, а не инженеру.
* `ix_client_identities_*` — фундамент docs/44, и запрет записан в самой модели
  (`app/models/client.py`). Один раз их уже ошибочно предлагали снести.
* `ix_conversations_claimed_by` — частичный индекс под внешний ключ; удаление
  безопасно лишь при уверенности, что хард-удаления пользователя не бывает.
* 32 уникальных индекса и первичные ключи с нулём сканов — это замки, а не
  пути доступа. Их «неиспользуемость» ничего не значит.
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

#: (имя, SQL создания) — чтобы `downgrade` возвращал ровно то, что снял.
МЁРТВЫЕ: tuple[tuple[str, str], ...] = (
    # 6688 кБ, самый крупный. Ни одного запроса с фильтром по account_id:
    # читатели `webhook_raw_log` ходят по received_at/error и по stream_id.
    (
        "ix_webhook_raw_account_received",
        "CREATE INDEX ix_webhook_raw_account_received ON webhook_raw_log (account_id, received_at)",
    ),
    # 400 кБ. Комментарий у объявления обещал «колокольчик: мои непрочитанные»,
    # но непрочитанность давно живёт в `notification_reads`, а не в `read_at`.
    (
        "ix_notifications_recipient_unread",
        "CREATE INDEX ix_notifications_recipient_unread "
        "ON notifications (recipient_id, created_at) WHERE read_at IS NULL",
    ),
    # 688 кБ, и перестраивается КАЖДЫЙ ЧАС при REFRESH витрины. `closed_at` в
    # витрине только проецируется — ни WHERE, ни ORDER BY, ни GROUP BY.
    (
        "mv_conversation_stats_closed_at",
        "CREATE INDEX mv_conversation_stats_closed_at ON mv_conversation_stats (closed_at)",
    ),
    # По 16 кБ. `undelivered_at` и `deleted_at` в коде только читаются как
    # атрибуты и записываются — в условиях выборки не встречаются нигде.
    (
        "ix_conversations_undelivered",
        "CREATE INDEX ix_conversations_undelivered ON conversations (undelivered_at)",
    ),
    ("ix_users_deleted", "CREATE INDEX ix_users_deleted ON users (deleted_at)"),
)


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_bot "
        "ON messages (conversation_id) WHERE sender_type = 'bot'"
    )
    for имя, _ in МЁРТВЫЕ:
        op.execute(f"DROP INDEX IF EXISTS {имя}")


def downgrade() -> None:
    for _, sql in МЁРТВЫЕ:
        op.execute(sql)
    op.execute("DROP INDEX IF EXISTS idx_messages_bot")
