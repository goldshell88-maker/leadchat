"""Три места, где база читала таблицу целиком (замеры боя 05.09).

ЧТО ИЗМЕРЕНО. Под каждым индексом лежит EXPLAIN (ANALYZE, BUFFERS) на боевых
данных, а не догадка по коду:

    счётчик колокольчика   → Seq Scan on notifications, 31 582 строки
                             отброшено фильтром, 7,8 мс, и так 25 раз в минуту
                             (790 тысяч лишних прочитанных строк в минуту)
    удаление диалога       → Seq Scan on clients, 61 364 строки отброшено,
                             11,1 мс, и так на каждое из 11 738 удалений
                             за месяц
    он же, вторая таблица  → Seq Scan on client_phone_candidates, 6 200 строк

ЗДЕСЬ ПРОВЕРЯЕТСЯ ДОГОВОР, А НЕ ПЛАН ЗАПРОСА. План зависит от объёма данных, и
на пустой тестовой базе PostgreSQL выберет перебор при любых индексах — такая
проверка была бы зелёной всегда. Проверяется другое: что индексы объявлены в
модели (иначе следующий autogenerate предложит их снести), что миграция их
заводит, и что форма индекса отвечает форме запроса.

⚠ ПОЧЕМУ ИНДЕКС ПО `recipient_id` ЛЕЧИТ ЗАПРОС С `OR`. Право видеть — «своё
ЛИБО рассылка на мою роль»: `recipient_id = я OR audience IN (…)`. Индекс на
рассылку был, на адресную часть — ни одного. `OR` с одной неиндексированной
стороной даёт полный перебор, сколько бы индексов ни висело на другой: чтобы
собрать BitmapOr, планировщику нужны обе.
"""

from __future__ import annotations

import pathlib

from app.models.client import Client, ClientPhoneCandidate
from app.models.notification import Notification

МИГРАЦИЯ = pathlib.Path(__file__).parents[2] / "app/db/migrations/versions/0067_missing_indexes.py"


def индексы(модель: type) -> dict[str, tuple[str, ...]]:
    return {i.name: tuple(c.name for c in i.columns) for i in модель.__table__.indexes}


def test_адресные_уведомления_проиндексированы() -> None:
    """⚠ ДИВЕРСИЯ: убрать Index из `Notification.__table_args__` — краснеет.

    Без этой половины `OR` счётчик колокольчика читает таблицу целиком 25 раз
    в минуту. Форма повторяет соседний индекс рассылки: `(поле, created_at)`.
    """
    имя = "ix_notifications_recipient_created"
    assert имя in индексы(Notification), "адресная половина права видеть без индекса"
    assert индексы(Notification)[имя] == ("recipient_id", "created_at")


def test_адресный_индекс_частичный() -> None:
    """Адресных уведомлений и рассылок примерно поровну.

    Частичность здесь не украшение: половина строк (рассылки, где
    `recipient_id IS NULL`) в индекс просто не попадает — и не оплачивается
    при каждой вставке.
    """
    индекс = next(i for i in Notification.__table__.indexes if "recipient_created" in i.name)
    условие = индекс.dialect_options["postgresql"].get("where")
    assert условие is not None and "recipient_id IS NOT NULL" in str(условие)


def test_дочерние_стороны_внешних_ключей_проиндексированы() -> None:
    """⚠ ДИВЕРСИЯ: убрать любой из двух Index — краснеет соответствующая строка.

    PostgreSQL не заводит индексы под внешние ключи сам, а проверять их при
    удалении родителя обязан. Цена в живом виде: чистка истории канала на
    2 700 диалогов — полминуты сплошных полных проходов ПОД БЛОКИРОВКАМИ.
    """
    assert индексы(Client).get("ix_clients_phone_conversation") == ("phone_conversation_id",)
    assert индексы(ClientPhoneCandidate).get("ix_client_phone_candidates_conversation") == (
        "conversation_id",
    )


def test_миграция_заводит_все_три() -> None:
    """Модель и база обязаны сойтись: объявленный, но не созданный индекс —
    это отсутствующий индекс с чистой совестью.
    """
    текст = МИГРАЦИЯ.read_text(encoding="utf-8")
    for имя in (
        "ix_notifications_recipient_created",
        "ix_clients_phone_conversation",
        "ix_client_phone_candidates_conversation",
    ):
        assert текст.count(имя) >= 2, f"{имя}: миграция обязана и создавать его, и уметь снять"


def test_миграция_не_трогает_сообщения() -> None:
    """⚠ ГРАНИЦА, А НЕ ПРИДИРКА. `messages` — 352 тысячи строк в 28 партициях,
    и обычный CREATE INDEX там держал бы блокировку на запись всё время
    построения, то есть остановил бы приём сообщений от клиентов. Три таблицы
    этой миграции меньше на два порядка (31, 61 и 6 тысяч строк), поэтому им
    обычного индекса достаточно. Появись здесь `messages` — понадобился бы
    CONCURRENTLY и отдельная миграция без транзакции.
    """
    текст = МИГРАЦИЯ.read_text(encoding="utf-8")
    строки_кода = [s for s in текст.splitlines() if not s.strip().startswith("#")]
    код = "\n".join(строки_кода).split('"""')[-1]
    assert '"messages"' not in код, "индекс по messages обычным CREATE INDEX остановит приём"
