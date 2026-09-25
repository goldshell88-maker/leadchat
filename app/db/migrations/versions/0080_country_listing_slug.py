"""Федеральные объявления: слаг «вся Россия» в колонке города — снять (19.09, B2).

`parse_listing_url` до этой выкатки считал первый сегмент `/all/…` и
`/rossiya/…` слагом города, и колонка получала «all»: шапка показывала
«город all», воркер карты считал город известным человеку
(`conversation_city_slug` не None → `geocode.ask_reason` молчит), город из
других диалогов клиента не брался. Теперь такой сегмент — не город
(`listing_url.COUNTRY_SEGMENTS`, слаг None). Здесь снимаем накопленное:
`conversation_city_slug` читает колонку раньше ссылки, и старое «all» иначе
пережило бы правку. Ссылка остаётся — downgrade восстанавливает слаг из неё.

Список слагов — копия `COUNTRY_SEGMENTS`, не импорт: миграция не должна менять
смысл задним числом при росте набора. Равенство стережёт
`tests/unit/test_paket2_federal_1909.py::test_миграция_снимает_те_же_слаги_что_разбор`.

`0079` — таблица `address_funnel_weekly` (снимок воронки адресов).
"""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None

СЛАГИ_СТРАНЫ: tuple[str, ...] = ("all", "rossiya")


def upgrade_sql() -> str:
    """Отдельной функцией: тест выполняет тот же SQL на sqlite."""
    список = ", ".join(f"'{s}'" for s in СЛАГИ_СТРАНЫ)
    return f"UPDATE conversations SET item_city_slug = NULL WHERE item_city_slug IN ({список})"


def upgrade() -> None:
    # По частичному индексу (item_city_slug, last_message_at) WHERE item_city_slug
    # IS NOT NULL (0024): десятки–сотни строк, миллисекунды.
    op.execute(upgrade_sql())


def downgrade() -> None:
    # Снимок «all» восстанавливается из ссылки без потерь; форма хоста — как у
    # `parse_listing_url` (AVITO_HOSTS + приставки www./m.).
    for слаг in СЛАГИ_СТРАНЫ:
        op.execute(
            sa.text(
                "UPDATE conversations SET item_city_slug = :slug "
                "WHERE item_city_slug IS NULL AND item_url ~* :re"
            ).bindparams(slug=слаг, re=rf"^(https?://)?(www\.|m\.)?(ru\.)?avito\.(ru|st)/{слаг}/")
        )
