"""Проверка адреса по карте: вердикт, строка в формате карт, связь карточки со строкой.

ПРОСЬБА ВЛАДЕЛЬЦА 11.09: поле адреса должно работать «полностью автоматически» —
само проверять адрес по картам в городе объявления и отдавать его в формате
Яндекс-карт («Звенигородская улица, 1, посёлок Заречный, Орск»).

ЧТО ДОБАВЛЯЕТСЯ К СТРОКЕ РАСПОЗНАВАНИЯ. Населённый пункт и город, названные
клиентом (`settlement`, `settlement_type`, `locality`) — они не входят в ключ
уникальности, чтобы уточнение посёлка в следующей реплике дописывалось в ту же
строку. И вердикт карты: `geo_status` с именованными отказами, `geo_formatted`,
координаты, провайдер, время и число попыток.

ЗАЧЕМ `clients.address_candidate_id`. В карточку теперь пишется строка карты, а
не «улица, дом» клиента — и четыре места, сравнивавшие текст поля со строками
кандидатов (`_address_source`, `_снять_совпавшие_адреса`, `identity_view`,
`inbound`), перестали бы сходиться. Связь по идентификатору — единственный
ответ «откуда адрес», и она заполняется для уже записанных адресов по равенству
текста: других строк на бою ещё не было.

СУЩЕСТВУЮЩИЕ СТРОКИ получают `geo_status='pending'`: их проверит починка
(`geo_repair`) в своём темпе. Автозапись их не коснётся — она берёт только
строки моложе суток.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

_GEO_STATUSES = (
    "'pending','exact','ambiguous','not_found','house_missing','house_mismatch',"
    "'street_mismatch','settlement_mismatch','city_mismatch','region_mismatch',"
    "'other_city_in_text','no_city','blocked','error'"
)


def upgrade() -> None:
    t = "client_address_candidates"
    op.add_column(t, sa.Column("settlement", sa.Text(), nullable=True))
    op.add_column(t, sa.Column("settlement_type", sa.Text(), nullable=True))
    op.add_column(t, sa.Column("locality", sa.Text(), nullable=True))
    # `server_default='pending'`: строка, вставленная старым кодом в окне между
    # миграцией и подменой контейнеров, тоже встаёт в очередь на проверку.
    op.add_column(t, sa.Column("geo_status", sa.Text(), nullable=True, server_default="pending"))
    op.add_column(t, sa.Column("geo_provider", sa.Text(), nullable=True))
    op.add_column(t, sa.Column("geo_formatted", sa.Text(), nullable=True))
    op.add_column(t, sa.Column("geo_lat", sa.Float(), nullable=True))
    op.add_column(t, sa.Column("geo_lon", sa.Float(), nullable=True))
    op.add_column(t, sa.Column("geo_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("geo_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint(
        "geo_status", t, f"geo_status IS NULL OR geo_status IN ({_GEO_STATUSES})"
    )
    # Починка ищет только то, что ещё не проверено или не ответило: частичный
    # индекс держит выборку дешёвой при любом росте таблицы.
    op.create_index(
        "ix_client_address_candidates_geo_todo",
        t,
        ["geo_status"],
        postgresql_where=sa.text("geo_status IN ('pending', 'error')"),
    )
    # Всё, что записано до этой правки, — в очередь на проверку.
    op.execute(f"UPDATE {t} SET geo_status = 'pending' WHERE geo_status IS NULL")

    op.add_column("clients", sa.Column("address_candidate_id", sa.Uuid(), nullable=True))
    op.add_column("clients", sa.Column("address_value", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_clients_address_candidate_id_client_address_candidates",
        "clients",
        t,
        ["address_candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Уже записанные адреса связываем со своей строкой по равенству текста —
    # ровно одной принятой; двух одинаковых быть не может (unique client_id,
    # value), а ручной ввод без строки остаётся без связи, и это верно.
    op.execute(
        f"""
        UPDATE clients c
           SET address_candidate_id = r.id,
               address_value = r.value
          FROM {t} r
         WHERE r.client_id = c.id
           AND r.status = 'accepted'
           AND c.address IS NOT NULL
           AND r.value = c.address
           AND c.address_candidate_id IS NULL
        """
    )
    _rename_legacy_checks(t)


def _rename_legacy_checks(t: str) -> None:
    """Имена CHECK из 0071 — под правило проекта, пока по ним никто не спотыкается.

    0071 создала ограничения с именами вида `ck_client_address_candidates_ck_…`
    (соглашение имён применилось поверх уже полного имени, хеш в хвосте).
    Следующая миграция по `status`/`level` написала бы `drop_constraint("status",
    …, type_="check")` и упала бы на бою — ровно то, на чём падала 0027 (см.
    0034). Имя берём из каталога, а не угадываем: хеш зависит от версии
    SQLAlchemy. Postgres-only: на SQLite миграции не идут.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(
        sa.text(
            """
            SELECT conname, pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'
            """
        ),
        {"t": t},
    ).fetchall()
    for conname, definition in rows:
        for column in ("status", "level"):
            wanted = f"ck_{t}_{column}"
            if definition.startswith(f"CHECK (({column} = ANY") and conname != wanted:
                op.execute(f'ALTER TABLE {t} RENAME CONSTRAINT "{conname}" TO "{wanted}"')


def downgrade() -> None:
    op.drop_constraint(
        "fk_clients_address_candidate_id_client_address_candidates", "clients", type_="foreignkey"
    )
    op.drop_column("clients", "address_candidate_id")
    op.drop_column("clients", "address_value")
    t = "client_address_candidates"
    op.drop_index("ix_client_address_candidates_geo_todo", table_name=t)
    op.drop_constraint("geo_status", t, type_="check")
    for col in (
        "geo_attempts",
        "geo_checked_at",
        "geo_lon",
        "geo_lat",
        "geo_formatted",
        "geo_provider",
        "geo_status",
        "locality",
        "settlement_type",
        "settlement",
    ):
        op.drop_column(t, col)
