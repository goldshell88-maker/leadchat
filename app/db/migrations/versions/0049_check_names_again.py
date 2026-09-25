"""Двойная приставка в именах CHECK — та же мина, что чинила 0034.

ЧТО ПРОИЗОШЛО. Правка 0034 объяснила ловушку: соглашение об именах
(`app/models/base.py`, ключ `ck` = `ck_%(table_name)s_%(constraint_name)s`)
применяется и к ЯВНО названным ограничениям — в отличие от pk/fk/uq. Значит
`name="ck_client_phone_candidates_status"` превращается в базе в
`ck_client_phone_candidates_ck_client_phone_candidates_status`.

Через две недели грабли легли обратно: 0037 и 0038 снова передали имена с
приставкой (`CK_STATUS = f"ck_{TABLE}_status"`), а модели ждут коротких —
`app/models/client.py` (`name="status"`) и `app/models/leadbot.py`
(`name="outcome"`). Пока никто не трогает эти словари, расхождение молчит; но
словарь `status` уже расширяли четырежды (0032/0033/0035/0036), и следующая
такая правка позовёт `DROP CONSTRAINT ck_client_phone_candidates_status` —
которого в базе нет. Деплой ляжет посреди окна выката, на боевой базе, и
ровно там, где на стенде всё зелено: стенд поднимается с нуля тем же 0037.

ЧИНИМ ТАК ЖЕ, КАК 0034: `RENAME CONSTRAINT` под проверкой существования
старого имени — правка каталога, мгновенно, без перепроверки данных; уже
исправленное руками не ломает деплой. Тексты 0037/0038 НЕ правим по той же
причине, что и там: они применены, а базы «до» и «после» правки разъехались
бы при одинаковом `alembic_version`.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels = None
depends_on = None

#: (таблица, как лежит в базе, как ждёт модель)
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "client_phone_candidates",
        "ck_client_phone_candidates_ck_client_phone_candidates_status",
        "ck_client_phone_candidates_status",
    ),
    (
        "leadbot_calls",
        "ck_leadbot_calls_ck_leadbot_calls_outcome",
        "ck_leadbot_calls_outcome",
    ),
)


def _rename_check(table: str, old: str, new: str) -> None:
    """RENAME под проверкой: имена — литералы этого файла, снаружи ничего."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = '{old}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite тестов имена ограничений так не хранит
    for table, old, new in RENAMES:
        _rename_check(table, old, new)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, old, new in RENAMES:
        _rename_check(table, new, old)
