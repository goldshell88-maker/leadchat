"""Доставка ответа: что она обязана сделать с состоянием диалога.

⚠ ОБРАТНАЯ СВЯЗЬ ОТ ДИСПЕТЧЕРОВ 02.09: «чат прочитан, отвечен, но тайминг висит
1 мин. Даже при обновлении страницы».

Ветка ПРОВАЛА доставки была написана внимательно: она возвращает отметку «клиент
ждёт» и ставит «не доставлено». Ветка УСПЕХА не делала ни того ни другого — и
путь «отправили → сорвалось → повторили → дошло» оставлял диалог вечно ждущим.
Обновление страницы не помогало: так стояло на сервере.

Здесь стерегут обе стороны пересчёта: гасить, когда ответ дошёл, и НЕ гасить,
когда клиент успел написать снова.
"""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Conversation, Message
from app.workers import deliver

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# Удавшаяся доставка гасит «клиент ждёт» (обратная связь диспетчеров 02.09)
# ---------------------------------------------------------------------------


async def test_успешная_доставка_гасит_ожидание(db_sessionmaker, redis, seed_conversation):
    """⚠ ОБРАТНАЯ СВЯЗЬ ОТ ДИСПЕТЧЕРОВ 02.09: «чат прочитан, отвечен, но тайминг
    висит 1 мин. Даже при обновлении страницы».

    ПУТЬ БЕДЫ. Нажали «Отправить» — отметка «клиент ждёт» погасла ещё до
    попытки доставки. Доставка сорвалась — ветка провала отметку ВЕРНУЛА, и это
    правильно: клиент ответа не получил. Повторили — дошло. А погасить отметку
    было больше некому: ветка успеха её не трогала вовсе.

    Диалог оставался вечно ждущим, обновление страницы не помогало — так стояло
    на сервере. Сторож «клиент ждёт 15 минут» при этом звенел по диалогу, на
    который уже ответили.

    ⚠ ПОЧЕМУ ЭТО ЧИНИТСЯ ПЕРЕСЧЁТОМ, А НЕ `awaiting_since = None`. Клиент мог
    написать снова, пока наш ответ шёл: тогда он ЖДЁТ, и гасить нельзя.
    Отметка вычисляется из переписки — граница по последнему доставленному
    ответу, — и вычисление одно на оба исхода доставки.
    """
    from app.services.messages import restore_awaiting

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        # Состояние после «провал → отметка вернулась».
        conv.awaiting_since = datetime.now(UTC) - timedelta(minutes=5)
        # А наш ответ в итоге доставлен.
        msg = Message(
            conversation_id=conv.id,
            direction="out",
            sender_type="operator",
            body="Да, надо посмотреть",
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC),
        )
        s.add(msg)
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        изменилось = await restore_awaiting(s, conv)
        await s.commit()

    assert изменилось, "пересчёт не тронул отметку — диалог останется «ждёт» навсегда"
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None and conv.awaiting_since is None, (
            "клиенту ответили и доставили, а диалог всё ещё числится ждущим"
        )


async def test_пересчёт_не_гасит_когда_клиент_написал_снова(
    db_sessionmaker, redis, seed_conversation
):
    """Обратная сторона: клиент написал, пока наш ответ шёл, — он ЖДЁТ.

    Спутай это в другую сторону — и диалог с непрочитанным вопросом клиента
    перестанет о себе напоминать. Тишина хуже лишнего счётчика.
    """
    from app.services.messages import restore_awaiting

    момент = datetime.now(UTC)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="ответ",
                attachments=[],
                delivery_status="delivered",
                created_at=момент - timedelta(minutes=3),
            )
        )
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="а ещё вопрос",
                attachments=[],
                delivery_status="delivered",
                created_at=момент - timedelta(minutes=1),
            )
        )
        conv.awaiting_since = None
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        await restore_awaiting(s, conv)
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None and conv.awaiting_since is not None, (
            "клиент написал после нашего ответа, а диалог не считается ждущим"
        )


def test_ветка_успеха_действительно_пересчитывает() -> None:
    """⚠ ПРОВОДКА. Без неё проверки выше стерегут функцию, которую никто не зовёт.

    Диверсия это и показала: убрал пересчёт из ветки успеха — все 95 проверок
    доставки и ожидания остались зелёными. Сам пересчёт покрыт, а вызов — нет.

    Гонять рабочий процесс доставки целиком тут дорого (Авито, ретраи, токены),
    поэтому проверяется исходник — тот же приём, что в
    `test_deliver_unexpected_error`: там так же стерегут ветку, до которой в
    тесте не дойти.
    """
    исходник = inspect.getsource(deliver)
    успех = исходник.split('msg.delivery_status = "delivered"', 1)
    assert len(успех) == 2, "ветка успешной доставки исчезла — проверять нечего"
    хвост = успех[1]

    assert "restore_awaiting" in хвост, (
        "успешная доставка не пересчитывает ожидание — диалог останется «ждёт» навсегда"
    )
    assert "refresh_undelivered" in хвост, (
        "успешная доставка не снимает «не доставлено» — строка останется красной"
    )
    assert "conversation_patch" in хвост, (
        "заплатка строки не уезжает на экран — счётчик погаснет только после F5"
    )
