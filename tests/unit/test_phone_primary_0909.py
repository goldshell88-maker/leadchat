"""Обмен основного номера с дополнительным — БЕЗ ПОТЕРИ прежнего.

⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «когда клиент даёт 2 номер, то исправить
можно только основной номер, второй изменить нельзя. Так же нельзя поменять их
местами».

Так и было. Второй номер живёт строкой принятого кандидата, и действий у неё не
было ни одного — карточка печатала «Ещё номера этого человека: …» обычным
текстом. Единственный способ поменять номера местами состоял в том, чтобы
перепечатать оба руками, и первый при этом ПРОПАДАЛ: `set_phone` прежний
затирает, оставляя след только в журнале аудита.

ГЛАВНОЕ, ЧТО СТЕРЕЖЁТСЯ ЗДЕСЬ, — ИМЕННО ОТСУТСТВИЕ ПОТЕРИ. Сам обмен проверить
легко и мало: если прежний номер после него не остался в карточке, правка сделала
ровно то, от чего оператор защищался, — и заметить это можно только тогда, когда
по второму номеру понадобится позвонить.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientPhoneCandidate, Conversation, Message
from app.models.client import (
    CANDIDATE_ACCEPTED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CANDIDATE_SOURCE_INBOUND,
    CANDIDATE_SOURCE_SWAP,
)
from app.services import clients as clients_svc

ОСНОВНОЙ = "+79125550177"
ВТОРОЙ = "+79995550188"


@pytest.fixture
async def карточка(seed_conversation, db_sessionmaker):
    """Клиент с основным номером и одним ПРИНЯТЫМ дополнительным.

    Ровно то состояние, в котором владелец и застал беду: клиент дал второй
    номер, оператор нажал «добавить», и дальше сделать с ним было нечего.
    """
    async with db_sessionmaker() as s:
        client = await s.get(Client, seed_conversation.client_id)
        assert client is not None
        client.phone = ОСНОВНОЙ
        s.add(
            ClientPhoneCandidate(
                client_id=client.id,
                conversation_id=seed_conversation.conversation_id,
                phone=ВТОРОЙ,
                raw="8 999 555 01 88",
                source=CANDIDATE_SOURCE_INBOUND,
                status=CANDIDATE_ACCEPTED,
                detected_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return seed_conversation


async def _обменять(client_http, token: str, client_id, body: dict):
    return await client_http.post(
        f"/api/v1/clients/{client_id}/phone/primary",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


async def _номера(db_sessionmaker, client_id) -> tuple[str | None, dict[str, str]]:
    """Основной номер и все строки кандидатов: номер → состояние."""
    async with db_sessionmaker() as s:
        client = await s.get(Client, client_id)
        rows = (
            (
                await s.execute(
                    sa.select(ClientPhoneCandidate).where(
                        ClientPhoneCandidate.client_id == client_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return (client.phone if client else None), {r.phone: r.status for r in rows}


async def test_обмен_меняет_основной(client, tokens, карточка, db_sessionmaker):
    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == ВТОРОЙ
    основной, _ = await _номера(db_sessionmaker, карточка.client_id)
    assert основной == ВТОРОЙ


async def test_прежний_основной_остаётся_в_карточке(client, tokens, карточка, db_sessionmaker):
    """⚠ САМАЯ ВАЖНАЯ ПРОВЕРКА ФАЙЛА: обмен не теряет номер.

    ДИВЕРСИЯ: убрать в `make_phone_primary` вызов `_keep_as_extra` — прежний
    номер исчезает из карточки совсем, и найти его можно только в журнале.
    Проверка краснеет здесь.
    """
    await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    основной, кандидаты = await _номера(db_sessionmaker, карточка.client_id)
    assert основной == ВТОРОЙ
    # Прежний основной обязан лежать ПРИНЯТЫМ: это номер человека, а не
    # предложение, которое кто-то должен рассмотреть.
    assert кандидаты.get(ОСНОВНОЙ) == CANDIDATE_ACCEPTED


async def test_у_удержанного_номера_видно_откуда_он(client, tokens, карточка, db_sessionmaker):
    """Происхождение не врёт: строка говорит «снят с основного», а не «распознан».

    Иначе карточка утверждала бы, что номер вычитан из переписки, а сообщения,
    из которого его вычитали, не существует.
    """
    await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                sa.select(ClientPhoneCandidate).where(
                    ClientPhoneCandidate.client_id == карточка.client_id,
                    ClientPhoneCandidate.phone == ОСНОВНОЙ,
                )
            )
        ).scalar_one()
    assert row.source == CANDIDATE_SOURCE_SWAP
    assert row.message_id is None  # сообщения нет, и выдумывать его нечем
    assert row.resolved_by_id is not None  # решение человека, а не системы
    # ⚠ И ДИАЛОГА У НЕЁ НЕТ — ЭТО ГЛАВНОЕ В СТРОКЕ (миграция 0070). Диалог
    # нажатия здесь был бы неправдой дважды: номер в той переписке не назывался,
    # а внешний ключ с ON DELETE CASCADE унёс бы строку вместе с диалогом —
    # тогда как `clients.phone` чистку канала переживает намеренно.
    assert row.conversation_id is None


async def test_прежде_отклонённый_номер_поднимается_а_не_двоится(
    client, tokens, карточка, db_sessionmaker
):
    """Оператор когда-то сказал «это не его номер», а номер оказался основным.

    Уникальность (client_id, phone) не даст вставить вторую строку, а прежнее
    решение обязано быть переписано: настоящее состояние — «его номер».
    """
    async with db_sessionmaker() as s:
        s.add(
            ClientPhoneCandidate(
                client_id=карточка.client_id,
                conversation_id=карточка.conversation_id,
                phone=ОСНОВНОЙ,
                raw=ОСНОВНОЙ,
                source=CANDIDATE_SOURCE_INBOUND,
                status=CANDIDATE_REJECTED,
                detected_at=datetime.now(UTC),
            )
        )
        await s.commit()

    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 200, r.text
    _, кандидаты = await _номера(db_sessionmaker, карточка.client_id)
    assert кандидаты[ОСНОВНОЙ] == CANDIDATE_ACCEPTED


async def test_обмен_пишет_журнал_со_своим_поводом(client, tokens, карточка, db_sessionmaker):
    await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "client.phone_edited")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details["by"] == "swap"
    assert rows[0].details["previous"] == ОСНОВНОЙ
    assert rows[0].details["phone"] == ВТОРОЙ


async def test_удержанный_номер_переживает_удаление_диалога(
    client, tokens, карточка, db_sessionmaker, engine
):
    """⚠ РАДИ ЭТОГО И ЗАВЕДЕНА МИГРАЦИЯ 0070.

    `clients.phone` чистку канала переживает НАМЕРЕННО («Клиентов НЕ трогаем:
    один и тот же человек мог писать в несколько каналов»), а строка кандидата
    с диалогом — нет: внешний ключ с ON DELETE CASCADE. Первая редакция обмена
    переносила прежний основной из первого места во второе, то есть создавала
    новый способ потерять номер ради того, чтобы его не терять. На бою событие
    `account.deleted` случалось 13 раз.

    ДИВЕРСИЯ: вернуть в `_keep_as_extra` привязку к диалогу нажатия —
    проверка краснеет.
    """
    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 200, r.text

    # SQLite внешние ключи по умолчанию не проверяет — включаем, иначе проверка
    # зеленела бы и без каскада, то есть мерила бы не то.
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.execute(
            sa.delete(Message).where(Message.conversation_id == карточка.conversation_id)
        )
        await conn.execute(
            sa.delete(Conversation).where(Conversation.id == карточка.conversation_id)
        )

    _, кандидаты = await _номера(db_sessionmaker, карточка.client_id)
    assert ОСНОВНОЙ in кандидаты, (
        f"прежний основной исчез вместе с диалогом; осталось: {sorted(кандидаты)}"
    )


@pytest.mark.parametrize("состояние", [CANDIDATE_PENDING, CANDIDATE_REJECTED])
async def test_нерассмотренный_и_отклонённый_основным_не_становятся(
    client, tokens, seed_conversation, db_sessionmaker, состояние
):
    """Обмен уважает решение человека, а не факт существования строки.

    `pending` — вопрос, на который оператор ещё не ответил; `rejected` — ответ
    «это не его номер». Сделать основным то, что человек либо не рассматривал,
    либо отверг, значит обойти его молча.
    """
    async with db_sessionmaker() as s:
        client_row = await s.get(Client, seed_conversation.client_id)
        assert client_row is not None
        client_row.phone = ОСНОВНОЙ
        s.add(
            ClientPhoneCandidate(
                client_id=client_row.id,
                conversation_id=seed_conversation.conversation_id,
                phone=ВТОРОЙ,
                raw=ВТОРОЙ,
                source=CANDIDATE_SOURCE_INBOUND,
                status=состояние,
                detected_at=datetime.now(UTC),
            )
        )
        await s.commit()

    r = await _обменять(
        client,
        tokens["admin"],
        seed_conversation.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(seed_conversation.conversation_id)},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unknown_phone"


async def test_происхождение_переезжает_вместе_с_номером(client, tokens, карточка, db_sessionmaker):
    """После обмена карточка обязана говорить «(из диалога)», а не «(со слов)».

    ⚠ ПОДПИСЬ ВЫБИРАЕТСЯ ПО `phone_set_by_id`/`phone_set_at`: они означают
    «номер НАБРАЛ ЧЕЛОВЕК С КЛАВИАТУРЫ». Обмен ничего не набирает — он выбирает
    из уже доказанного. Поставь их — и один и тот же номер получал бы разное
    происхождение в зависимости от нажатой кнопки.

    ДИВЕРСИЯ: вернуть `client.phone_set_by_id = actor.id` — проверка краснеет.
    """
    await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    async with db_sessionmaker() as s:
        row = await s.get(Client, карточка.client_id)
    assert row is not None
    # Отметка «набрал человек» описывает ТЕКУЩИЙ номер, и обмен её сбрасывает:
    # оставить её значило бы приписать рукописный ввод номеру, который его не
    # видел (разбор — в шапке `make_phone_primary`).
    assert row.phone_set_by_id is None
    assert row.phone_set_at is None
    # Диалог и канал берутся у строки, которая номер ДОКАЗЫВАЕТ.
    assert row.phone_conversation_id == карточка.conversation_id
    assert row.phone_account_id == карточка.account.id


async def test_первое_заполнение_обменом_попадает_в_метрику(
    client, tokens, seed_conversation, db_sessionmaker
):
    """Пустая карточка + обмен = «собрано телефонов», а не «исправлено».

    Метрика 06 §1.4 считает ТОЛЬКО `client.phone_captured`. Без развилки первый
    номер карточки, поставленный обменом, выпал бы из отчёта молча.
    """
    async with db_sessionmaker() as s:
        client_row = await s.get(Client, seed_conversation.client_id)
        assert client_row is not None
        client_row.phone = None
        s.add(
            ClientPhoneCandidate(
                client_id=client_row.id,
                conversation_id=seed_conversation.conversation_id,
                phone=ВТОРОЙ,
                raw=ВТОРОЙ,
                source=CANDIDATE_SOURCE_INBOUND,
                status=CANDIDATE_ACCEPTED,
                detected_at=datetime.now(UTC),
            )
        )
        await s.commit()

    r = await _обменять(
        client,
        tokens["admin"],
        seed_conversation.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(seed_conversation.conversation_id)},
    )
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.entity == "client")))
            .scalars()
            .all()
        )
    assert [x.action for x in rows] == ["client.phone_captured"]
    # Разбивка отчёта читает `details.source`: без него строка попала бы в итог
    # и выпала бы из всех трёх колонок сразу.
    assert rows[0].details["source"] == "regex"
    assert rows[0].details["conversation_id"] == str(seed_conversation.conversation_id)


async def test_обратный_обмен_не_объявляет_рукописный_номер_распознанным(
    client, tokens, карточка, db_sessionmaker
):
    """⚠ КАПКАН, НАЙДЕННЫЙ РАЗБОРОМ: У НОМЕРА ПОЯВЛЯЕТСЯ СВОЯ СТРОКА ОБМЕНА.

    Поменяли местами туда и обратно. Прежний основной (он был набран руками)
    успел обзавестись строкой `source='swap'` со статусом `accepted` — и если
    считать доказательством любую принятую строку, карточка объявит рукописный
    номер «(из диалога)», а в `phone_account_id` подставит канал, которого
    Авито не присылало. На этой колонке стоит межканальная сверка.

    ДИВЕРСИЯ: убрать в `make_phone_primary` фильтр `source != swap` — проверка
    краснеет на подписи.
    """
    async with db_sessionmaker() as s:
        row = await s.get(Client, карточка.client_id)
        assert row is not None
        # Прежний основной — рукописный: у него есть автор и время.
        row.phone_set_by_id = row.phone_set_by_id or None
        row.phone_set_at = datetime.now(UTC)
        await s.commit()

    туда = {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)}
    обратно = {"phone": ОСНОВНОЙ, "conversation_id": str(карточка.conversation_id)}
    assert (await _обменять(client, tokens["admin"], карточка.client_id, туда)).status_code == 200
    # ⚠ ПОЛОВИНА ПЕРВАЯ: отметка ручного ввода не имеет права достаться новому
    # номеру. До правки она оставалась, и распознанный в переписке ВТОРОЙ
    # подписывался бы «(со слов)» — ложь, унаследованная от прежнего номера.
    async with db_sessionmaker() as s:
        середина = await s.get(Client, карточка.client_id)
    assert середина is not None and середина.phone == ВТОРОЙ
    assert середина.phone_set_at is None

    r = await _обменять(client, tokens["admin"], карточка.client_id, обратно)
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        итог = await s.get(Client, карточка.client_id)
    assert итог is not None
    assert итог.phone == ОСНОВНОЙ
    # Доказательства из переписки у этого номера нет — значит нет ни диалога,
    # ни канала. Подпись карточки станет «(источник неизвестен)», и это правда.
    assert итог.phone_conversation_id is None
    assert итог.phone_account_id is None

    личность = await clients_svc.identity_view(
        *(await _для_личности(db_sessionmaker, карточка.client_id))
    )
    assert личность["phone_source"] == "other"


async def _для_личности(db_sessionmaker, client_id):
    """Сессия и карточка для `identity_view` — она принимает их парой."""
    s = db_sessionmaker()
    row = await s.get(Client, client_id)
    return s, row


@pytest.mark.parametrize("роль", ["admin", "head", "manager"])
async def test_обмен_доступен_каждому_кто_ведёт_диалоги(client, tokens, карточка, роль):
    """ALLOW-ветка права: сузишь его — тринадцать человек молча получат 403.

    Отказ наблюдателю проверяется соседним случаем; вместе они и есть замена
    строке матрицы, которой у этой ручки быть не может.
    """
    r = await _обменять(
        client,
        tokens[роль],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 200, r.text


async def test_незнакомый_номер_отвергается(client, tokens, карточка):
    """⚠ ЭТО НЕ ВТОРОЙ СПОСОБ ВПИСАТЬ ЛЮБОЙ НОМЕР.

    Для произвольного номера есть `PUT /phone` со своим разбором, своим
    журналом и своей метрикой. Два пути к одному полю в этом проекте расходятся
    с завидным постоянством, поэтому здесь принимается только то, что за
    человеком уже числится.
    """
    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": "+79001110022", "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unknown_phone"


async def test_повторное_нажатие_ничего_не_меняет(client, tokens, карточка, db_sessionmaker):
    """Кнопка могла приехать из устаревшего кадра карточки — это не ошибка.

    Отдельной строки журнала повтор тоже не заводит: одна опечатка, поправленная
    трижды, не должна выглядеть как три обмена.
    """
    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ОСНОВНОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is False
    async with db_sessionmaker() as s:
        сколько = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "client.phone_edited")
            )
        ).scalar_one()
    assert сколько == 0


async def test_чужой_диалог_в_теле_отвергается(client, tokens, карточка, db_sessionmaker):
    """Диалог обязан быть ЭТОГО клиента: иначе «где это было» непроверяемо.

    Аккаунт берём ТОТ ЖЕ, что у карточки: отличаться обязан клиент, а не канал,
    и второй аккаунт только упёрся бы в уникальность `avito_user_id`.
    """
    async with db_sessionmaker() as s:
        чужой = Client(channel="avito", external_id="999002", name="Другой")
        s.add(чужой)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=карточка.account.id,
            client_id=чужой.id,
            status="new",
        )
        s.add(conv)
        await s.commit()
        чужой_диалог = conv.id

    r = await _обменять(
        client,
        tokens["admin"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(чужой_диалог)},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "conversation_mismatch"


async def test_наблюдатель_не_меняет_основной(client, tokens, карточка):
    """Право то же, что у правки телефона руками: `conversations:manage`."""
    r = await _обменять(
        client,
        tokens["observer"],
        карточка.client_id,
        {"phone": ВТОРОЙ, "conversation_id": str(карточка.conversation_id)},
    )
    assert r.status_code == 403, r.text
