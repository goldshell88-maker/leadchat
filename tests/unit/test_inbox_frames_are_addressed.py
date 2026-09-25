"""Кадр очереди всегда уезжает со списком допущенных (27.08).

⚠ ДЕФЕКТ, РАДИ КОТОРОГО ЭТО НАПИСАНО. Хаб отбирает получателей `inbox:new` по
ключу `eligible`, который кладёт ТОЛЬКО публикатор: `publish_inbox_new` пишет
его, лишь если ему передали `eligible_operator_ids`. Пустой список по правилу
совместимости означает «канал открыт всем» (`_channel_allows`, ws/hub.py).

Боевой конвейер вебхука список не передавал ни в одной из трёх своих веток, и
кадр очереди чужого канала уезжал ВСЕМ тринадцати диспетчерам: звук на чужого
клиента, строка с живой кнопкой «Принять», 403 при нажатии, а после
перезагрузки строка исчезала — счётчик расходился со списком.

⚠ ПОЧЕМУ НЕ ПОЙМАЛ СУЩЕСТВУЮЩИЙ СТОРОЖ. `test_notification_catalog` ищет
строки, где рядом стоят `publish_event(` и `INBOX_NEW`. Вызовы вида
`publish_inbox_new(redis, frame)` под это условие не подходят вовсе — тест был
зелёным, а дыра открытой. Здесь проверяются ИМЕННО такие вызовы.
"""

import ast
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2] / "app"


#: Помощники, кадр которых хаб отбирает по списку допущенных.
#:
#: ⚠ `inbox:released` ДОБАВЛЕН 28.08, И ЭТО БЫЛА ЖИВАЯ ДЫРА. Хаб отбирает
#: получателей обоих типов одним и тем же ключом — `_allowed`: «if t in
#: (INBOX_NEW, INBOX_RELEASED)». Все семь публикаторов `inbox:new` список
#: передавали, а все три публикатора `inbox:released` собирали словарь руками и
#: ключа не клали вовсе. Сторож смотрел только на `inbox:new` и был зелёным:
#: оператор возвращал диалог в очередь, и строка чужого клиента со звонком
#: уезжала каждому подключённому менеджеру, а «Принять» отвечало 403.
ПОМОЩНИКИ = ("publish_inbox_new", "publish_inbox_released")


def _вызовы() -> list[tuple[str, int, bool]]:
    """(файл, строка, передан ли список допущенных) для каждого публикатора очереди."""
    out: list[tuple[str, int, bool]] = []
    for путь in sorted(КОРЕНЬ.rglob("*.py")):
        if "migrations" in путь.parts:
            continue
        дерево = ast.parse(путь.read_text(encoding="utf-8"))
        for узел in ast.walk(дерево):
            if not isinstance(узел, ast.Call):
                continue
            имя = getattr(узел.func, "id", None) or getattr(узел.func, "attr", None)
            if имя not in ПОМОЩНИКИ:
                continue
            адресован = any(k.arg == "eligible_operator_ids" for k in узел.keywords)
            out.append((str(путь.relative_to(КОРЕНЬ.parent)), узел.lineno, адресован))
    return out


ВЫЗОВЫ = _вызовы()


def test_calls_are_found_at_all():
    """Сторож бесполезен, если разбор перестал находить вызовы."""
    assert len(ВЫЗОВЫ) >= 5, f"нашлось всего {len(ВЫЗОВЫ)} вызовов — разбор сломался"
    файлы = {f for f, _, _ in ВЫЗОВЫ}
    assert any("routes/inbox.py" in f for f in файлы), (
        "среди вызовов нет ручки возврата в очередь — либо разбор сломался, "
        "либо кадр снова публикуется голым `publish_event`"
    )


@pytest.mark.parametrize("файл,строка", [(f, n) for f, n, _ in ВЫЗОВЫ])
def test_every_inbox_frame_is_addressed(файл: str, строка: int):
    адресован = next(a for f, n, a in ВЫЗОВЫ if (f, n) == (файл, строка))
    assert адресован, (
        f"{файл}:{строка} — публикатор очереди без `eligible_operator_ids`. "
        "Кадр уедет ВСЕМ операторам, включая тех, кому канал не назначен: "
        "чужой клиент зазвенит, «Принять» ответит 403. Собирайте кадр через "
        "`inbox.inbox_frame_addressed` и передавайте список."
    )


def test_released_frame_cannot_forget_the_list():
    """У `publish_inbox_released` список ОБЯЗАТЕЛЕН и без умолчания.

    Умолчание `None` вернуло бы ровно ту дыру: вызов собирается, ключ не
    кладётся, кадр уезжает всем. Проверяем подпись, а не текст.
    """
    import inspect

    from app.ws.hub import publish_inbox_released

    параметр = inspect.signature(publish_inbox_released).parameters["eligible_operator_ids"]
    assert параметр.default is inspect.Parameter.empty, (
        "у списка допущенных появилось умолчание — забыть его снова стало можно"
    )


def test_addressed_helper_returns_both_halves():
    """Пара «кадр + список» — единственный способ не разъехаться снова."""
    import inspect

    from app.services.inbox import inbox_frame_addressed

    тело = inspect.getsource(inbox_frame_addressed)
    assert '"conversation"' in тело and '"eligible"' in тело
