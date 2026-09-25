"""«Сообщение удалено» — серая запись Авито, а не реплика клиента.

Авито не объявляет удалённое сообщение видом (`type: deleted` в сырце не
встречается ни разу), а подменяет текст словами «Сообщение удалено». Такие
записи ложились репликой клиента — 1012 за всё время: пузырь в ленте,
непрочитанное, ожидание ответа. С 12.09 адаптер узнаёт их по слову и пишет
серой записью; здесь то же делается с накопленным.

Партиционирование `messages` не мешает: меняются колонки вне ключа секции.
"""

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

_DELETED = "Сообщение удалено"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE messages
           SET direction = 'system', sender_type = 'avito', body = 'Клиент удалил сообщение'
         WHERE direction = 'in' AND sender_type = 'client' AND body = '{_DELETED}'
           AND attachments = '[]'::jsonb
        """
    )
    op.execute(
        f"""
        UPDATE messages
           SET direction = 'system', sender_type = 'avito', body = 'Сотрудник удалил сообщение'
         WHERE direction = 'out' AND body = '{_DELETED}' AND attachments = '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE messages
           SET direction = 'in', sender_type = 'client', body = '{_DELETED}'
         WHERE direction = 'system' AND sender_type = 'avito' AND body = 'Клиент удалил сообщение'
        """
    )
    op.execute(
        f"""
        UPDATE messages
           SET direction = 'out', sender_type = 'operator', body = '{_DELETED}'
         WHERE direction = 'system' AND sender_type = 'avito'
           AND body = 'Сотрудник удалил сообщение'
        """
    )
