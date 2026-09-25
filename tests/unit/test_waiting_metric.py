"""Одна метрика ожидания на весь продукт (CHATS-07 / FUNC-14).

ЧТО ОХРАНЯЮТ ЭТИ ТЕСТЫ. 12 августа на одном экране «сколько клиент ждёт» было
показано пятью способами и тремя разными числами сразу: «ждёт 2 ч» в шапке,
шкала «7ч» в строке списка, «в очереди 7 ч 11 мин» в карточке, «Диалог ждёт в
очереди» внизу и «Ждал 6 ч 41 мин» в системной записи. Каждое число само по
себе выглядело правдоподобным — тем и опасно: диспетчер не может знать, какому
верить, и перестаёт верить всем.

Теперь расчёт один (`conversation_status.waiting_since`), и наружу уезжает одно
поле `waiting_since`. Ниже — четыре случая, на которых прежние расчёты врали, и
граница определения: от чего метрика считается и от чего НЕ считается.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.models import Conversation, Message
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services import inbox

# Время «сейчас» в разборе аналитика: диалог приняли из очереди в 14:27.
NOW = datetime(2026, 8, 12, 14, 27, tzinfo=UTC)


def _conv(**kw) -> Conversation:
    """Диалог, в котором клиент ждёт с 12:00. Поля перекрываются по месту."""
    defaults = {
        "id": uuid.uuid4(),
        "channel": "avito",
        "external_chat_id": "wait-1",
        "account_id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "status": "in_progress",
        "unread_count": 1,
        "awaiting_since": NOW - timedelta(hours=2, minutes=27),  # 12:00
        "last_message_at": NOW - timedelta(hours=2, minutes=27),
    }
    defaults.update(kw)
    return Conversation(**defaults)


def _incoming(at: datetime) -> Message:
    """Сообщение КЛИЕНТА — то, от чего метрика и обязана считаться."""
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        direction="in",
        sender_type="client",
        body="ну что там?",
        attachments=[],
        delivery_status="delivered",
        created_at=at,
    )


def _outgoing(at: datetime, *, delivery_status: str = "delivered") -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        direction="out",
        sender_type="operator",
        body="сейчас уточню",
        attachments=[],
        delivery_status=delivery_status,
        created_at=at,
    )


# ------------------------------------------------------------------ что гасит


def test_closed_conversation_does_not_wait():
    """Закрытый диалог не ждёт — разговор окончен, торопить некого.

    Это не теория: закрытие `unread_count` не трогало, а прежняя шкала в списке
    считалась по нему — и закрытое обращение показывало «ждёт 3 ч» рядом с
    нижней плашкой «Диалог закрыт. Вернуть в работу». Два утверждения об одном
    диалоге, противоречащие друг другу, на расстоянии полуэкрана.
    """
    conv = _conv(status="closed")
    assert status_dict.waiting_since(conv) is None


def test_snoozed_conversation_does_not_wait():
    """Отложенный — тоже: он вернётся сам в назначенный срок.

    Отдельным тестом, а не строчкой в предыдущем: закрытие и отложка приходят
    из разных мест кода, и погасить ожидание мог бы кто-то один.
    """
    conv = _conv(status="snoozed", snoozed_until=NOW + timedelta(hours=3))
    assert status_dict.waiting_since(conv) is None


def test_imported_history_does_not_wait():
    """Залитая история молчит, хотя последним в ней писал клиент.

    Предикат — серверная отметка `awaiting_since`, и импорт её НЕ ставит
    (`inbound`, ветка `backfill`). Считай мы «ждёт» по направлению последнего
    сообщения, все 454 тысячи импортированных диалогов разом зажглись бы
    просрочкой — и очередь на экране перестала бы что-либо значить.
    """
    conv = _conv(awaiting_since=None)
    started = status_dict.waiting_since(conv, last_client_message_at=NOW - timedelta(days=200))
    assert started is None


# --------------------------------------------------------- что НЕ должно гасить


def test_opening_the_conversation_does_not_reset_the_wait():
    """Оператор открыл диалог — ожидание продолжается.

    ПРЕЖНЯЯ БЕДА. Шкала в строке списка требовала `unread_count > 0`, а
    открытие диалога обнуляет счётчик локально, ещё до ответа сервера. Значит
    ожидание гасло ровно в ту секунду, когда оператор заглянул в диалог и НИЧЕГО
    не ответил, — то есть именно тогда, когда оно нужнее всего. Клиент ждал
    дальше, а система об этом молчала.

    Непрочитанное — состояние ОПЕРАТОРА. Ожидание — состояние КЛИЕНТА. Метрика
    не имеет права смотреть на первое.
    """
    conv = _conv(unread_count=0)
    assert status_dict.waiting_since(conv) == NOW - timedelta(hours=2, minutes=27)


def test_failed_reply_keeps_the_wait():
    """Ответ, который не ушёл, ответом не считается.

    `awaiting_since` снимается в момент нажатия «Отправить», до попытки
    доставки; провалившуюся отправку возвращает `messages.restore_awaiting`.
    Метрика обязана уважать это восстановление, даже если последним в превью
    лежит наше исходящее: клиент наших слов не видел и ждёт по-прежнему.
    """
    conv = _conv(undelivered_at=NOW - timedelta(minutes=30))
    started = status_dict.waiting_since(
        conv,
        # Последним в ленте лежит наше сообщение — но упавшее, и подсказки о
        # клиенте у вызывающего нет.
        last_client_message_at=None,
    )
    assert started == NOW - timedelta(hours=2, minutes=27)


# ------------------------------------------------------------ от чего считается


def test_anchor_is_the_last_client_message_not_the_first():
    """Отсчёт — от ПОСЛЕДНЕГО сообщения клиента, а не от первого.

    Клиент написал в 12:00, мы промолчали, он написал снова в 14:00. Отметка
    `awaiting_since` осталась на 12:00 (она ставится один раз — это верно для
    сторожа, который обязан сработать раньше). На экране же от первого
    сообщения получается «ждёт 2 ч 27 мин» вместо «27 мин»: число выглядит
    правдоподобно и врёт вдвое.
    """
    conv = _conv()
    started = status_dict.waiting_since(conv, last_client_message_at=NOW - timedelta(minutes=27))
    assert started == NOW - timedelta(minutes=27)


def test_our_own_message_does_not_move_the_anchor():
    """Наше исходящее якорь не двигает — иначе «ждёт 2 минуты» после нас самих.

    Подсказка передаётся только для входящих (см. `conversation_out`); здесь
    проверяется, что без неё расчёт остаётся на серверной отметке, а не
    съезжает на «последнее сообщение вообще».
    """
    conv = _conv()
    assert status_dict.waiting_since(conv) == NOW - timedelta(hours=2, minutes=27)


def test_hint_earlier_than_the_mark_is_ignored():
    """Подсказка старше отметки не откатывает ожидание назад.

    Такое приходит из превью строки: последним видимым сообщением может лежать
    входящее ДО того, как ожидание перезапустилось. Брать меньшее из двух
    значило бы завышать ожидание без всякого основания.
    """
    conv = _conv()
    started = status_dict.waiting_since(conv, last_client_message_at=NOW - timedelta(hours=9))
    assert started == NOW - timedelta(hours=2, minutes=27)


def test_naive_timestamps_do_not_crash_the_comparison():
    """Метки без зоны сравниваются, а не роняют расчёт.

    Наивное время приходит из SQLite (юнит-тесты) и из строк, записанных до
    перевода колонок в `timezone=True`. Без приведения сравнение падало бы
    TypeError ровно в одном из двух диалектов — то есть только на бою.
    """
    conv = _conv(awaiting_since=datetime(2026, 8, 12, 12, 0))  # без зоны
    started = status_dict.waiting_since(conv, last_client_message_at=NOW - timedelta(minutes=27))
    assert started == NOW - timedelta(minutes=27)


# ------------------------------------------------- возврат в очередь пересчитывает


def test_return_to_queue_recomputes_the_wait():
    """РАЗБОР АНАЛИТИКА. Диалог вернулся в очередь в 12:08, принят в 14:27 —
    и системная запись сообщила «Ждал 3 ч 43 мин».

    Три часа сорок три минуты — от первого попадания в очередь (10:44), хотя
    между 10:44 и 12:08 оператор клиенту ответил, и клиент написал заново в
    11:55. С 24.09 возврат ставит `offered_at` от нынешнего ожидания клиента,
    и число в ленте, эскалация и место в очереди считаются от него.
    """
    first_message = NOW - timedelta(hours=3, minutes=43)  # 10:44
    client_wrote_again = NOW - timedelta(hours=2, minutes=32)  # 11:55
    returned_at = NOW - timedelta(hours=2, minutes=19)  # 12:08

    conv = _conv(
        status="in_progress",
        assignee_id=uuid.uuid4(),
        claimed_by_id=uuid.uuid4(),
        offered_at=first_message,
        awaiting_since=client_wrote_again,
    )
    inbox.return_to_queue(conv, now=returned_at)

    assert conv.offered_at == client_wrote_again
    # А число для человека считается от клиента, и оно другое.
    started = status_dict.waiting_since(conv, last_client_message_at=client_wrote_again)
    assert started == client_wrote_again
    assert NOW - started == timedelta(hours=2, minutes=32)
    # И БЕЗ ПОДСКАЗКИ ТОЖЕ. Отдельной строкой, потому что подсказка есть не
    # всегда (в превью может лежать наше исходящее), а `offered_at` соблазнителен
    # именно там: он у строки под рукой и выглядит как «время ожидания».
    assert status_dict.waiting_since(conv) == client_wrote_again
    assert _out(conv)["waiting_since"] == "2026-08-12T11:55:00.000Z"


# ------------------------------------------------------------- поле в ответе API


def _out(conv: Conversation, last_message: Message | None = None) -> dict:
    return convs.conversation_out(
        conv, account=None, client=None, assignee=None, last_message=last_message
    )


def test_response_carries_the_single_field():
    """Поле едет в КАЖДОЙ строке — и в списке, и в детали, и в кадре WS.

    Уехало бы только в детали — шкала в строке списка осталась бы на
    самодельном расчёте, и разнобой вернулся бы на следующий же день.
    """
    conv = _conv()
    assert _out(conv)["waiting_since"] == "2026-08-12T12:00:00.000Z"


def test_response_says_null_when_nobody_waits():
    """«Не ждёт» — это `null`, а не отсутствие поля.

    Отличать «сервер сказал, что не ждёт» от «строка старая, поля нет» умеет
    только интерфейс, и умеет он это по `null` против `undefined`: у первого он
    гасит счётчик, у второго откатывается к запасному расчёту.
    """
    assert _out(_conv(status="closed"))["waiting_since"] is None


def test_response_anchors_on_the_last_client_message():
    """В строке списка якорь берётся из уже загруженного превью.

    Отдельного запроса за последним входящим здесь нет и быть не должно:
    `messages` партиционирована (06 §0.2), а строк на странице пятьдесят.
    """
    conv = _conv()
    out = _out(conv, _incoming(NOW - timedelta(minutes=27)))
    assert out["waiting_since"] == "2026-08-12T14:00:00.000Z"


def test_response_ignores_our_outgoing_in_the_preview():
    """Наше исходящее в превью якорь не двигает.

    Иначе диалог, в котором оператор написал минуту назад и получил отказ
    доставки, показывал бы «ждёт 1 мин» — вместо честных двух с половиной часов.
    """
    conv = _conv()
    out = _out(conv, _outgoing(NOW - timedelta(minutes=1), delivery_status="failed"))
    assert out["waiting_since"] == "2026-08-12T12:00:00.000Z"
