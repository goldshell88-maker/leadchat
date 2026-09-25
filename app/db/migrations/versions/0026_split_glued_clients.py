"""Расцепить карточки клиентов, слипшиеся по пустому идентификатору.

ЧТО СЛУЧИЛОСЬ. 11 августа включили две вещи разом: загрузку ВСЕЙ истории
канала и показ служебных событий Авито. У служебного события («Пользователь
посмотрел номер из объявления», «Пользователь создал чат, но пока ничего не
написал») автора нет, а путь переписки заводит клиента ПО АВТОРУ: `str(None)`
и `str(0)` давали "None" и "0", и отбор `channel + external_id` находил первую
такую карточку.

К ней прицепился каждый следующий такой чат. На боевой системе 12 августа:
ОДНА карточка с именем «Иван Петрович Иванов» держала ВОСЕМЬ диалогов
из восьми городов (Армавир, Дзержинск, Абакан, Коммунар, Гатчина, Балаково,
Одинцово) и с ОБОИХ каналов. Оператор, открыв любой из них, видел чужое имя и
историю из восьми чужих обращений. Вдобавок карточка получила подпись
«возможно, этот же человек писал и на другой наш канал» — при том, что совпало
ровно ничего.

ЧТО ДЕЛАЕТ МИГРАЦИЯ. Разбивает каждую такую карточку по диалогам: один диалог
— одна карточка с запасным идентификатором `chat:<external_chat_id>`. Первый
диалог остаётся на исходной строке (чтобы не переписывать её id в ссылках), у
неё же сбрасывается всё, что было унаследовано ложно: имя, телефон, признаки
межканальной склейки.

ЧЕГО МИГРАЦИЯ НЕ ДЕЛАЕТ. Не пытается восстановить настоящие имена: их у нас
нет, а выдуманное имя хуже пустого. Имя подтянется само, когда в чат придёт
настоящее сообщение или когда задача обогащения дотянет карточку чата.

ОБРАТИМОСТЬ. Обратной операции нет и быть не может: склейка уничтожила
сведения о том, кто где был. `downgrade` поэтому пустой — и это честнее, чем
обещание вернуть то, чего мы не знаем.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

#: Значения, которые НИКОГДА не были идентификатором человека.
_GARBAGE = ("0", "", "None", "null")


def upgrade() -> None:
    bind = op.get_bind()

    glued = (
        bind.execute(
            sa.text(
                """
            SELECT c.id AS client_id, c.channel
            FROM clients c
            WHERE c.external_id = ANY(:garbage)
            """
            ),
            {"garbage": list(_GARBAGE)},
        )
        .mappings()
        .all()
    )

    if not glued:
        return

    moved = 0
    for row in glued:
        convs = (
            bind.execute(
                sa.text(
                    """
                SELECT id, external_chat_id
                FROM conversations
                WHERE client_id = :cid
                ORDER BY external_chat_id
                """
                ),
                {"cid": row["client_id"]},
            )
            .mappings()
            .all()
        )
        if not convs:
            continue

        # Первый диалог остаётся на исходной карточке: так у неё сохраняются
        # заметки и ссылки, а переписывать нечего.
        first, rest = convs[0], convs[1:]
        bind.execute(
            sa.text(
                """
                UPDATE clients
                   SET external_id = :ext,
                       name = NULL,
                       phone = NULL,
                       cross_account_since = NULL,
                       link_confidence = NULL,
                       link_phone_conflict_at = NULL,
                       phone_account_id = NULL
                 WHERE id = :cid
                """
            ),
            {"ext": f"chat:{first['external_chat_id']}", "cid": row["client_id"]},
        )

        for conv in rest:
            new_id = bind.execute(
                sa.text(
                    """
                    INSERT INTO clients (id, channel, external_id, name, phone)
                    VALUES (gen_random_uuid(), :channel, :ext, NULL, NULL)
                    RETURNING id
                    """
                ),
                {"channel": row["channel"], "ext": f"chat:{conv['external_chat_id']}"},
            ).scalar_one()
            bind.execute(
                sa.text("UPDATE conversations SET client_id = :new WHERE id = :conv"),
                {"new": new_id, "conv": conv["id"]},
            )
            moved += 1

    # Сообщения от «клиента», которые на деле служебные, помечаются заново:
    # разбор истории теперь ставит им is_system, но уже сохранённые строки
    # об этом не знают. Признак — наш собственный префикс, который ставит
    # `_system_text`; ничем другим он в переписке не встречается.
    bind.execute(
        sa.text(
            """
            UPDATE messages
               SET sender_type = 'avito', direction = 'system'
             WHERE direction = 'in'
               AND sender_type = 'client'
               AND body LIKE '[Системное сообщение]%'
            """
        )
    )

    # Ожидание и непрочитанное, посчитанные по служебным строкам, — неправда.
    bind.execute(
        sa.text(
            """
            UPDATE conversations c
               SET unread_count = 0
             WHERE NOT EXISTS (
                     SELECT 1 FROM messages m
                      WHERE m.conversation_id = c.id
                        AND m.direction = 'in'
                        AND m.sender_type = 'client'
                   )
            """
        )
    )

    print(f"расцеплено карточек: {len(glued)}, диалогов переведено: {moved}")  # noqa: T201


def downgrade() -> None:
    """Обратной операции нет: склейка уничтожила сведения о том, кто где был.

    Пустой downgrade здесь честнее исключения: откат схемы на 0025 остаётся
    возможным, а данные назад не склеиваются — и не должны.
    """
