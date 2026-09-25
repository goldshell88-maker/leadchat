"""Охрана единого словаря статусов (docs/38 §0).

Словарь ОДИН, но записан он неизбежно в нескольких синтаксисах: питоновский
кортеж, `CheckConstraint` строкой SQL, `Literal` в схеме запроса, регулярка
фильтра и объединение типов TypeScript. Ни один из них нельзя собрать из
другого — `Literal` требует констант времени импорта, ограничение уезжает в
миграцию текстом, а фронт не ходит на сервер ради отрисовки фильтра.

Значит дубли остаются, и вопрос только в том, разъедутся они молча или с
криком. Эти тесты — про крик.
"""

import pathlib
import re

import pytest

from app.api.routes import conversations as conv_routes
from app.models import Conversation
from app.services import audit, distribution, inbox
from app.services import conversation_status as status_dict
from app.services import conversation_table as tbl
from app.services import conversations as convs

FRONT = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend/src/shared/lib/conversationStatus.ts"
)


def _front_array(name: str) -> list[str]:
    """Значения массива/множества из общего словаря фронта — текстом.

    Файл читается как текст, а не импортируется: питон не исполняет
    TypeScript. Приём в проекте уже принят — так же охраняется список
    сортируемых колонок (`test_conversation_table.py`).
    """
    ts = FRONT.read_text(encoding="utf-8")
    m = re.search(rf"{name}[^=]*=\s*(?:new Set<[^>]+>\()?\[(.*?)\]", ts, re.S)
    assert m, f"во фронте пропал экспорт {name}"
    return re.findall(r'"([^"]+)"', m.group(1))


def _front_labels(name: str) -> dict[str, str]:
    ts = FRONT.read_text(encoding="utf-8")
    m = re.search(rf"{name}: Record<ConversationStatus, string> = \{{(.*?)\}};", ts, re.S)
    assert m, f"во фронте пропал словарь {name}"
    return dict(re.findall(r'(\w+): "([^"]+)"', m.group(1)))


def test_frontend_knows_the_same_statuses():
    """Серверный `STATUSES` и фронтовый `STATUS_ORDER` — один список, в одном порядке.

    Порядок важен наравне с составом: он же порядок пунктов во всех фильтрах, и
    разъехавшийся порядок означает, что «Статус ▾» в чатах и в «Разборе
    диалогов» перечисляют одно и то же по-разному.
    """
    assert _front_array("STATUS_ORDER") == list(status_dict.STATUSES), (
        "словарь статусов разъехался между сервером и фронтом"
    )


def test_frontend_knows_the_same_wait_rule():
    """Правило «при каком статусе показывать счётчик ожидания» — общее.

    Их два экземпляра: серверный гасит `awaiting_since`, фронтовый гасит шкалу
    в списке и счётчик в шапке. Разъедутся — вернётся дефект №3: закрытый
    диалог с непрочитанным снова покажет «ждёт 3 ч».
    """
    assert set(_front_array("STATUS_SHOWS_WAIT")) == set(status_dict.STATUS_SHOWS_WAIT)


def test_frontend_labels_match_the_server():
    """Подписи в единственном числе совпадают дословно.

    Не косметика: этими подписями сервер печатает системную запись в ленте
    («Статус: Новый → В работе») и колонку отчёта, а фронт — поле «Статус» в
    карточке. Разные слова об одном состоянии на соседних экранах — то, с чего
    у этого продукта и начались девять словарей.
    """
    assert _front_labels("STATUS_LABEL") == status_dict.LABELS
    assert _front_labels("STATUS_LABEL_PLURAL") == status_dict.LABELS_PLURAL


def test_check_constraint_lists_the_same_statuses():
    """`CHECK` в модели перечисляет ровно словарь.

    Ограничение уезжает в миграцию текстом SQL, поэтому собрать его из кортежа
    нельзя. Забытое там значение — это `IntegrityError` на боевом трафике в
    тот момент, когда оператор впервые выберет новый статус.
    """
    checks = [c for c in Conversation.__table__.constraints if hasattr(c, "sqltext")]
    status_check = [c for c in checks if "status IN" in str(c.sqltext)]
    assert status_check, "с conversations.status пропал CheckConstraint"
    listed = set(re.findall(r"'(\w+)'", str(status_check[0].sqltext)))
    assert listed == set(status_dict.STATUSES)


def test_request_schema_accepts_exactly_the_dictionary():
    """`Literal` схемы `PATCH /status` совпадает со словарём.

    Уже — и ручка отвергнет статус, который интерфейс показывает; шире — и в
    базу поедет значение, которого не знает `CHECK`.
    """
    literal = conv_routes.StatusPatch.model_fields["status"].annotation
    assert set(literal.__args__) == set(status_dict.STATUSES)


def test_list_filter_pattern_accepts_exactly_the_dictionary():
    """Регулярка фильтра `GET /conversations` — тот же список.

    Именно она отвергала бы новые значения с 400 раньше, чем их успел бы
    выбрать человек.
    """
    for code in status_dict.STATUSES:
        assert re.match(conv_routes.STATUS_FILTER_PATTERN, code), f"фильтр не пускает {code}"
    assert not re.match(conv_routes.STATUS_FILTER_PATTERN, "waiting")


@pytest.mark.parametrize(
    "where, labels",
    [
        ("services.conversations", convs.STATUS_LABELS),
        ("services.audit", audit._STATUS_LABELS),
        ("services.conversation_table", tbl.STATUS_RU),
    ],
)
def test_no_service_keeps_its_own_labels(where, labels):
    """Три бывшие копии подписей — теперь один и тот же объект.

    Проверяется ТОЖДЕСТВО, а не равенство: равные словари можно получить и
    двумя копиями, которые разойдутся завтра. Один объект разойтись не может.
    """
    assert labels is status_dict.LABELS, f"{where} завёл свою копию подписей"


def test_queue_constants_are_from_the_dictionary():
    assert {inbox.NEW, inbox.IN_PROGRESS, inbox.CLOSED} <= set(status_dict.STATUSES)


def test_load_counts_only_what_needs_attention_now():
    """Нагрузка автораздачи — НЕ «всё незакрытое».

    Диалог, ждущий клиента, внимания сейчас не требует, и человек с двадцатью
    такими обязан считаться свободным. Пока список был «всё, кроме закрытых»,
    следующее обращение доставалось бы тому, у кого их просто нет.
    """
    assert distribution.OPEN_STATUSES == status_dict.ACTIVE_STATUSES
    assert "waiting_client" not in distribution.OPEN_STATUSES


def test_the_snoozed_status_is_gone_from_every_copy_of_the_dictionary():
    """ОХРАНА РЕШЕНИЯ ВЛАДЕЛЬЦА ОТ 12 АВГУСТА: отложки нет ни в одной копии.

    Словарь статусов записан в продукте пятью синтаксисами, которые нельзя
    вывести друг из друга (см. шапку файла). Соседние тесты сверяют их МЕЖДУ
    СОБОЙ — то есть пропустили бы согласованный возврат `snoozed` во все пять
    сразу. Этот проверяет ОТСУТСТВИЕ поимённо, и обойти его молча нельзя.

    Почему отдельным тестом, а не строкой в соседнем: сообщение при падении
    должно говорить не «списки разъехались», а «функцию вернули — так решил
    владелец?». Это разные разговоры.
    """
    assert "snoozed" not in status_dict.STATUSES
    assert "snoozed" not in status_dict.LABELS
    assert "snoozed" not in status_dict.LABELS_PLURAL
    assert "snoozed" not in status_dict.TRANSITIONS
    assert all("snoozed" not in dsts for dsts in status_dict.TRANSITIONS.values())
    assert not re.match(conv_routes.STATUS_FILTER_PATTERN, "snoozed")
    literal = conv_routes.StatusPatch.model_fields["status"].annotation
    assert "snoozed" not in literal.__args__
    assert "snoozed" not in _front_array("STATUS_ORDER")
    # Колонки при этом на месте: снос необратим, а решение может измениться.
    assert {"snoozed_until", "snoozed_by_id", "snooze_reason"} <= set(
        Conversation.__table__.columns.keys()
    )


def test_open_statuses_are_everything_but_closed():
    assert set(status_dict.OPEN_STATUSES) == set(status_dict.STATUSES) - {"closed"}


def test_every_transition_leads_to_a_known_status():
    """Матрица переходов не ссылается на несуществующее значение.

    Опечатка в `TRANSITIONS` дала бы переход, который разрешён и невозможен, —
    и человек получал бы отказ базы вместо отказа с объяснением.
    """
    for src, dsts in status_dict.TRANSITIONS.items():
        assert src in status_dict.STATUSES, src
        assert dsts <= set(status_dict.STATUSES), (src, dsts)
    assert set(status_dict.TRANSITIONS) == set(status_dict.STATUSES)


def test_every_forbidden_transition_has_a_reason():
    """У КАЖДОГО запрета есть свой текст.

    «Нельзя» без объяснения оператор читает как поломку и идёт к
    администратору. Общий текст остаётся только для сочетаний, которых в
    матрице нет вовсе, — то есть для ошибки сопровождения.
    """
    for src in status_dict.STATUSES:
        for dst in status_dict.STATUSES:
            if src == dst or status_dict.can_transition(src, dst):
                continue
            reason, message = status_dict.denial(src, dst)
            assert reason and message


def test_worked_statuses_never_include_the_queue():
    """«Ждут ответа» считается по ведущимся диалогам, а не по всем открытым.

    Отметка `awaiting_since` стоит и у диалогов очереди. Попади они в это
    множество — карточка «Ждут ответа» снова стала бы суммой очереди и работы,
    то есть вернулся бы дефект STATS-01: руководитель принимал неразобранную
    очередь за невыполненную работу диспетчеров.
    """
    assert "new" not in status_dict.WORKED_STATUSES
    assert set(status_dict.WORKED_STATUSES) <= set(status_dict.OPEN_STATUSES)


def test_my_today_widget_counts_the_same_worked_statuses():
    """Виджет оператора и карточка руководителя считают ОДНО множество.

    Числа обязаны сходиться цифра в цифру при одинаковых фильтрах (06 §6.2).
    SQL виджета перечисляет статусы литералами — иначе его не написать, — и
    этот тест сторожит, чтобы литералы совпадали со словарём.
    """
    from app.services import stats

    listed = set(re.findall(r"c\.status IN \(([^)]+)\)", stats._MY_TODAY_SQL))
    assert listed, "в SQL виджета пропал фильтр по статусам"
    for group in listed:
        codes = set(re.findall(r"'(\w+)'", group))
        assert codes <= set(status_dict.STATUSES), codes
    # Ровно множество «ведущихся» — оно и есть «мои активные».
    assert any(
        set(re.findall(r"'(\w+)'", group)) == set(status_dict.WORKED_STATUSES) for group in listed
    ), "виджет «моя статистика» считает не то множество, что карточка руководителя"
