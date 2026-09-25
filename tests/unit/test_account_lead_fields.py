"""Источник, номер партнёра и ссылка на отзыв у канала — правятся, а не только читаются.

⚠ ЭТИ КОЛОНКИ ЖИЛИ С МИГРАЦИИ 0040 И ЗАПОЛНИТЬ ИХ БЫЛО НЕЧЕМ. Заявка их читает —
`review_url` уходит в комментарий (`lead_comment.build`), `lead_partner_number` решает
галочку «Отзыв» (`lead_flags.wants_review`), `lead_origin` уезжает в «Комментарий
Партнера», — но ни схема правки, ни экран о них не знали ни разу. Оставалась правка в
базе руками.

Владелец 14 августа, дословно: «я не могу указать источник, который будет использовать
при автосоздании заявки, и ссылку на отзыв».

ПОЧЕМУ ЭТО ПРОЖИЛО. Тестов на «поле можно записать» не бывает по привычке: пишут на
поведение. А здесь поведение — заявка — читало значения, которые в бою всегда пусты, и
вело себя ровно так же, как если бы их не было. Молчаливый прочерк не отличается от
молчаливого умолчания.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _правка(client, token, account_id, **поля):
    return await client.patch(
        f"/api/v1/avito-accounts/{account_id}",
        json=поля,
        headers={"Authorization": f"Bearer {token}"},
    )


class TestПоляЗаявкиУКанала:
    async def test_все_три_поля_сохраняются_и_возвращаются(
        self, client, tokens, make_avito_account
    ):
        account = await make_avito_account(7000001, title="Дамир")
        r = await _правка(
            client,
            tokens["admin"],
            account.id,
            lead_origin="В95",
            lead_partner_number="723",
            review_url="https://otzyv.example/damir",
        )
        assert r.status_code == 200, r.text
        тело = r.json()
        assert тело["lead_origin"] == "В95"
        assert тело["lead_partner_number"] == "723"
        assert тело["review_url"] == "https://otzyv.example/damir"

    async def test_пустая_строка_снимает_значение(self, client, tokens, make_avito_account):
        """Иначе очистить поле было бы нечем: `null` в JSON означает «не трогать»."""
        account = await make_avito_account(7000002, title="Тимофей")
        await _правка(client, tokens["admin"], account.id, lead_origin="В95")
        r = await _правка(client, tokens["admin"], account.id, lead_origin="")
        assert r.status_code == 200, r.text
        assert r.json()["lead_origin"] is None

    async def test_не_указанное_поле_не_трогается(self, client, tokens, make_avito_account):
        """⚠ Частичная правка обязана быть частичной.

        Экран сохраняет поля по одному, по уходу фокуса. Затирай PATCH остальные —
        и заполнение второго поля стирало бы первое, причём молча.
        """
        account = await make_avito_account(7000003, title="Канал")
        await _правка(client, tokens["admin"], account.id, lead_origin="В95")
        r = await _правка(client, tokens["admin"], account.id, review_url="https://o.example")
        assert r.status_code == 200, r.text
        assert r.json()["lead_origin"] == "В95", "правка одного поля стёрла соседнее"
        assert r.json()["review_url"] == "https://o.example"

    async def test_наблюдателю_править_канал_нельзя(self, client, tokens, make_avito_account):
        account = await make_avito_account(7000004, title="Канал")
        r = await _правка(client, tokens["observer"], account.id, lead_origin="В95")
        assert r.status_code == 403


class TestПоляВШапкеЧата:
    """Партнёр и источник обязаны доехать до диалога — их читает шапка чата.

    ⚠ ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ТЕСТ. Полей два, имена похожи, а смысл разный:
    `lead_src_key` — В КАКОЙ лид-центр уедет заявка (bt/kp/mnc), `lead_origin` —
    КАКОЙ КОД источника в ней проставить («В95»). Первая редакция карточки
    канала записывала источник в лид-центр, и сервер честно отвечал
    «Неизвестный лид-центр» — владелец увидел это первым же сохранением.
    """

    async def test_партнёр_и_источник_едут_в_диалог(
        self, client, tokens, make_avito_account, db_sessionmaker
    ):
        import uuid as _uuid
        from datetime import UTC, datetime

        from app.models import Client, Conversation

        account = await make_avito_account()
        await _правка(
            client, tokens["admin"], account.id, lead_origin="В95", lead_partner_number="007"
        )

        async with db_sessionmaker() as db:
            cl = Client(id=_uuid.uuid4(), channel="avito", external_id="777333")
            db.add(cl)
            await db.flush()
            conv = Conversation(
                id=_uuid.uuid4(),
                channel="avito",
                external_chat_id="chat-lead-fields",
                account_id=account.id,
                client_id=cl.id,
                status="new",
                bot_active=False,
                bot_vars={},
                tags=[],
                unread_count=0,
                declined_by=[],
                last_message_at=datetime.now(UTC),
            )
            db.add(conv)
            await db.commit()
            conv_id = conv.id

        ответ = await client.get(
            f"/api/v1/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert ответ.status_code == 200, ответ.text
        канал = ответ.json()["account"]
        assert канал["lead_partner_number"] == "007"
        assert канал["lead_origin"] == "В95", (
            "источник обязан доехать до шапки чата — иначе его снова впишут в имя канала"
        )


class TestLeadFieldsAudit:
    """Правка полей заявки канала попадает в журнал (проверка 24.09).

    Комментарий обещал «общую запись ниже», а её не было: кто поменял источник
    или партнёра — от которых зависят «белые» заявки и галочка «Отзыв», —
    ответить было нечем.
    """

    async def test_a_change_is_logged_with_before_and_after(
        self, client, tokens, make_avito_account, db_sessionmaker
    ):
        import sqlalchemy as sa

        from app.models import AuditLog
        from app.services import audit

        account = await make_avito_account(7000011, title="Канал")
        await _правка(client, tokens["admin"], account.id, lead_origin="В95")
        r = await _правка(
            client, tokens["admin"], account.id, lead_origin="В59", lead_partner_number="723"
        )
        assert r.status_code == 200, r.text

        async with db_sessionmaker() as s:
            rows = (
                (
                    await s.execute(
                        sa.select(AuditLog)
                        .where(AuditLog.action == "account.lead_fields_changed")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
        assert rows[-1].details == {
            "changes": {
                "lead_origin": {"from": "В95", "to": "В59"},
                "lead_partner_number": {"from": None, "to": "723"},
            }
        }
        assert audit.describe(rows[-1].action, rows[-1].details) == (
            "Изменены поля заявки канала (источник: В95 → В59; партнёр: — → 723)"
        )

    async def test_the_same_value_writes_no_row(
        self, client, tokens, make_avito_account, db_sessionmaker
    ):
        import sqlalchemy as sa

        from app.models import AuditLog

        account = await make_avito_account(7000012, title="Канал")
        await _правка(client, tokens["admin"], account.id, review_url="https://o.example")
        await _правка(client, tokens["admin"], account.id, review_url="https://o.example")

        async with db_sessionmaker() as s:
            count = (
                await s.execute(
                    sa.select(sa.func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "account.lead_fields_changed")
                )
            ).scalar_one()
        assert count == 1
