"""Разбор очереди командой: каждый диалог в свою кучу (боевой случай 19.08).

ЧТО СЛУЧИЛОСЬ. Загрузка истории считала свежесть по ЛЮБОМУ последнему событию,
включая наш собственный ответ. В очередь попали диалоги, где мы уже ответили и
ход давно за клиентом: владелец увидел это как «некоторые диалоги висят
47 дней». Источник починен, но накопленное надо разобрать — а закрывать всё
подряд нельзя: среди этих строк есть живая работа.

ПОЧЕМУ ЭТО КОМАНДА, А НЕ ЗАПРОС В БАЗУ. Запрос никто не проверит и никто не
повторит: он живёт в переписке и умирает вместе с ней. Команда проверяется
тестом, называет числа ДО того как что-то менять, и пишет в журнал аудита, что
именно сделала.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.cli import triage_queue
from app.models import AuditLog, Client, Conversation, Message

СЕЙЧАС = datetime.now(UTC)


async def _диалог(db, account, *, ключ: str, реплики: list[tuple[str, datetime]]) -> uuid.UUID:
    """Диалог в очереди с заданной перепиской: ('in'|'out', когда)."""
    client = Client(id=uuid.uuid4(), channel="avito", external_id=f"cl-{ключ}")
    db.add(client)
    await db.flush()
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=ключ,
        account_id=account.id,
        client_id=client.id,
        status="new",
        offered_at=СЕЙЧАС - timedelta(days=1),
        bot_active=False,
        bot_vars={},
        tags=[],
        unread_count=1,
        declined_by=[],
        last_message_at=реплики[-1][1],
    )
    db.add(conv)
    await db.flush()
    for направление, когда in реплики:
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction=направление,
                sender_type="client" if направление == "in" else "operator",
                body="текст",
                delivery_status="delivered",
                created_at=когда,
            )
        )
    await db.commit()
    return conv.id


async def test_каждый_диалог_попадает_в_свою_кучу(db_sessionmaker, make_avito_account, monkeypatch):
    """Три случая — три судьбы, и ни один не перепутан."""
    account = await make_avito_account()
    async with db_sessionmaker() as db:
        ответили = await _диалог(  # ход за клиентом: мы ответили последними
            db,
            account,
            ключ="ответили",
            реплики=[("in", СЕЙЧАС - timedelta(days=3)), ("out", СЕЙЧАС - timedelta(days=2))],
        )
        протух = await _диалог(  # клиент спросил и молчит месяц
            db,
            account,
            ключ="протух",
            реплики=[("in", СЕЙЧАС - timedelta(days=30))],
        )
        живой = await _диалог(  # клиент спросил вчера и ждёт
            db,
            account,
            ключ="живой",
            реплики=[("in", СЕЙЧАС - timedelta(days=1))],
        )

    # Команда ходит своей сессией и своим циклом событий: подменяем `_run`,
    # чтобы выполнить её тело на тестовой сессии.
    import app.cli as cli_mod

    async def выполнить(apply: bool) -> None:
        собранное = []
        monkeypatch.setattr(cli_mod, "_run", lambda main: собранное.append(main))
        triage_queue(apply=apply, stale_days=14)
        async with db_sessionmaker() as db:
            await собранное[0](db)

    await выполнить(apply=False)
    async with db_sessionmaker() as db:  # показ ничего не меняет
        statuses = {
            c.external_chat_id: c.status for c in (await db.execute(select(Conversation))).scalars()
        }
    assert statuses == {"ответили": "new", "протух": "new", "живой": "new"}

    await выполнить(apply=True)
    async with db_sessionmaker() as db:
        строки = {c.external_chat_id: c for c in (await db.execute(select(Conversation))).scalars()}
        записи = list((await db.execute(select(AuditLog))).scalars())

    assert строки["ответили"].status == "waiting_client", "мы ответили — ход за клиентом"
    assert строки["ответили"].offered_at is None, "из очереди убрали"
    assert строки["ответили"].awaiting_since is None, "ждём клиента, а не он нас"

    assert строки["протух"].status == "closed", "месяц молчания — ответ уже неуместен"
    assert строки["протух"].unread_count == 0

    assert строки["живой"].status == "new", "вчерашний вопрос — это работа на сегодня"
    assert строки["живой"].offered_at is not None

    разбор = [z for z in записи if z.action == "conversation.bulk_triaged"]
    assert len(разбор) == 1, "разбор обязан оставить след в журнале"
    assert разбор[0].details["waiting_client"] == 1
    assert разбор[0].details["closed"] == 1
    assert разбор[0].details["left"] == 1
    # Проверяем именно те диалоги, что завели: идентификаторы совпадают.
    assert {строки["ответили"].id, строки["протух"].id, строки["живой"].id} == {
        ответили,
        протух,
        живой,
    }


def test_команда_видна_в_списке_команд():
    """Команда, объявленная ПОСЛЕ запуска приложения, не существует.

    ⚠ ТАК И ВЫШЛО (19.08). Я дописал `triage-queue` в конец файла — а в конце
    файла стоит `if __name__ == "__main__": app()`. Python выполняет модуль
    сверху вниз: до объявления команды дело не доходило, приложение уже
    запустилось. Владелец получил «No such command 'triage-queue'» на боевом
    сервере, дважды.

    Тест держит не расположение строк, а само свойство: команда есть в списке.
    Любая следующая, дописанная в конец, упадёт здесь, а не у человека.
    """
    from app.cli import app as cli_app

    имена = {c.name for c in cli_app.registered_commands}
    assert "triage-queue" in имена, (
        'команда не зарегистрирована — скорее всего, объявлена после if __name__ == "__main__"'
    )
    # Заодно проверяем, что и остальные на месте: если запуск снова уедет
    # наверх, отвалится сразу пачка, и причина будет видна.
    assert {"create-admin", "seed-smoke", "history-status"} <= имена
