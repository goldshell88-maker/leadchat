"""Сторожа, которые ОТБИРАЮТ диалог, берут строку под замок.

⚠ ЧЕМ ЭТО КОНЧАЕТСЯ БЕЗ ЗАМКА. Между выборкой кандидатов и записью у джобы
стоят запрос присутствия, запрос по сообщениям, сбор кадра очереди и аудит.
Данные, по которым принимается решение (ответственный, статус,
`auto_assigned_at`), к концу пачки успевают устареть на секунды, а `UPDATE` ORM
идёт БЕЗ единого условия на прежние значения — оптимистичной блокировки
(`version_id_col`) в проекте нет вовсе.

Все человеческие пути берут строку через `get_conversation_for_update`
(08 §8.4), но замок не мешает сторожу ЧИТАТЬ мимо него: его `UPDATE` просто
дожидается чужого commit'а и ложится сверху. Классический lost update —
затираются `assignee_id`, `status`, `claimed_by_id`, `offered_at`, а в журнал
уходит `conversation.reclaimed` с причиной «оператор не в сети», уже неправдой.

Событие не случайное: администратор передаёт диалог ушедшего сотрудника ПОТОМУ,
что тот ушёл, — человек и сторож реагируют на один и тот же факт и попадают в
одну минуту чаще, чем дают независимые вероятности.

⚠ ПРОВЕРЯЕМ ПО ДЕРЕВУ, А НЕ ПО СТРОКЕ. На SQLite `FOR UPDATE` молча
игнорируется, поэтому поведенческой проверки здесь не построить: замок обязан
быть виден В КОДЕ. Разбор `ast` переживает и `ruff format`, и перенос условий.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.scheduler.jobs import bot_stuck as bot_stuck_mod
from app.scheduler.jobs import reclaim as reclaim_mod

#: Функции, которые ЗАБИРАЮТ диалог у человека или переигрывают его решение.
#:
#: `check_awaiting` и прочие напоминалки сюда не входят намеренно: они ничего не
#: отбирают, а пишут уведомление — гонка там стоит лишнего письма, а не потери
#: работы.
ОТБИРАЮЩИЕ = [
    reclaim_mod.reclaim_in_session,
    reclaim_mod.expire_transfers,
    bot_stuck_mod.release_in_session,
]


def _выборки_диалогов(функция: object) -> list[bool]:
    """Для каждого `select(Conversation)` — взят ли он под замок."""
    дерево = ast.parse(textwrap.dedent(inspect.getsource(функция)))
    итог: list[bool] = []
    for узел in ast.walk(дерево):
        if not isinstance(узел, ast.Call):
            continue
        имя = getattr(узел.func, "attr", None) or getattr(узел.func, "id", None)
        if имя != "select":
            continue
        про_диалоги = any(isinstance(a, ast.Name) and a.id == "Conversation" for a in узел.args)
        if not про_диалоги:
            continue
        # Поднимаемся по цепочке .where(...).limit(...).with_for_update(...)
        цепочка = _цепочка_вокруг(дерево, узел)
        итог.append("with_for_update" in цепочка)
    return итог


def _цепочка_вокруг(дерево: ast.AST, начало: ast.Call) -> set[str]:
    """Имена методов, навешенных на этот `select(...)` по цепочке."""
    имена: set[str] = set()
    for узел in ast.walk(дерево):
        if not (isinstance(узел, ast.Call) and isinstance(узел.func, ast.Attribute)):
            continue
        # Метод принадлежит цепочке, если внутри его получателя лежит наш select.
        for внутри in ast.walk(узел.func.value):
            if внутри is начало:
                имена.add(узел.func.attr)
                break
    return имена


@pytest.mark.parametrize("функция", ОТБИРАЮЩИЕ, ids=lambda f: f.__name__)
def test_выборка_кандидатов_берёт_строку_под_замок(функция: object) -> None:
    замки = _выборки_диалогов(функция)
    assert замки, f"{функция.__name__} больше не выбирает диалоги — обнови список"
    assert all(замки), (
        f"{функция.__name__}: выборка диалогов без `with_for_update` — решение и запись "
        "перестали быть одним куском, и правка человека будет затёрта без следа"
    )


def test_замок_пропускает_занятую_строку_а_не_ждёт_её() -> None:
    """`skip_locked` обязателен: без него сторож встаёт в очередь за человеком.

    Ждать чужую транзакцию сторожу нельзя — он держал бы всю пачку и опаздывал
    бы к остальным диалогам. Занятую строку правильно пропустить до следующего
    прохода: через минуту она либо освободится, либо уже не будет кандидатом.
    """
    for функция in ОТБИРАЮЩИЕ:
        источник = textwrap.dedent(inspect.getsource(функция))
        for кусок in источник.split("with_for_update")[1:]:
            assert кусок.lstrip().startswith("(skip_locked=True)"), (
                f"{функция.__name__}: замок без `skip_locked` — сторож будет ждать человека"
            )
