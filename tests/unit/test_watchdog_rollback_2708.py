"""Упавшая проверка сторожа не выключает остальные (27.08).

⚠ ОБЕЩАНИЕ ДЕРЖАЛОСЬ НА ЧЕСТНОМ СЛОВЕ. Докстринг `run_watchdog` говорит:
«Упавшая проверка не уносит с собой остальные: сторож, который умирает от
первой же ошибки, — это сторож, которого нет». Держалось это на `except
Exception: … continue`, и отката там не было — во всём файле не было ни одного
`rollback`.

Между тем все двенадцать проверок бегут по ОДНОЙ сессии, а `session_scope`
транзакцию не открывает и не закрывает: откат делает только выход из области. В
PostgreSQL любой упавший запрос переводит транзакцию в aborted, и каждый
следующий SELECT по той же сессии получает «current transaction is aborted».

То есть первая же упавшая проверка молча выключала все остальные: в журнале одна
строка `watchdog.check_failed` вместо двенадцати, и понять, что сторож ослеп
целиком, было не по чему.
"""

import inspect

from app.scheduler.jobs import watchdog


def test_failed_check_rolls_the_session_back():
    тело = inspect.getsource(watchdog.run_watchdog)
    ветка = тело[тело.index("watchdog.check_failed") :]
    assert "db.rollback()" in ветка, (
        "без отката транзакция остаётся aborted, и все следующие проверки "
        "падают на пустом месте — сторож слепнет целиком"
    )


def test_rollback_failure_is_not_swallowed():
    """Сессия могла умереть вместе с соединением. Молчать здесь нельзя: это уже
    другая беда, и в журнале она обязана быть названа своим именем."""
    тело = inspect.getsource(watchdog.run_watchdog)
    assert "watchdog.rollback_failed" in тело


def test_the_promise_is_still_written_down():
    """Сторож защищает не строку кода, а обещание докстринга — если обещание
    уберут, эта проверка должна перестать иметь смысл вместе с ним."""
    док = inspect.getdoc(watchdog.run_watchdog) or ""
    assert "не уносит с собой остальные" in док
