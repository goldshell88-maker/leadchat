"""ИМЯ ПОЛЯ ОЖИДАНИЯ В КАДРАХ — ТО, КОТОРОЕ ЧИТАЕТ ЭКРАН.

⚠ ЧТО БЫЛО. Сервер знает `conversations.awaiting_since` — сырую отметку в
базе. Экран считает шкалу по `waiting_since` — каноническому расчёту
(`conversation_status.waiting_since`, зеркало во `frontend/src/shared/lib/
waiting.ts`), и поля с именем `awaiting_since` во фронте нет НИ ОДНОГО.

Разницу в одну букву не видно глазом, и она стоила двух молчаливых поломок
сразу:

* кадр провала доставки годами вёз `awaiting_since` — то есть восстановленное
  ожидание не доезжало до строки списка никогда, и сообщение, не ушедшее
  клиенту, оставалось без тревоги до полной перезагрузки;
* проверка на это существовала и зеленела: она сверяла ровно то бесполезное
  имя, которое сервер и слал.

Отсюда заслон на весь класс, а не на одно место: в кадры наружу сырое имя не
уезжает. Пусть новый кадр либо назовёт поле канонически, либо споткнётся здесь.

ГРАНИЦА НАМЕРЕННО УЗКАЯ — только имя в кадре. Модель, запросы, сторожа и
миграции работают с `awaiting_since` и обязаны продолжать: это поле базы.
"""

import re
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[2] / "app"

#: Где собираются кадры наружу. Смотрим только их: сырое имя в запросе или в
#: модели — норма, а вот в кадре — поломка.
КАДРЫ = (
    "app/services/messages.py",
    "app/services/inbound.py",
    "app/workers/deliver.py",
    "app/api/routes/conversations.py",
    "app/api/routes/inbox.py",
    "app/api/routes/messages.py",
    "app/services/inbox.py",
    "app/bots/handoff.py",
)


def строки(путь: Path) -> list[str]:
    return путь.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("файл", КАДРЫ)
def test_no_raw_awaiting_key_in_outgoing_frames(файл: str) -> None:
    путь = КОРЕНЬ.parent / файл
    if not путь.exists():  # файл переехал — это не повод молчать
        pytest.fail(f"{файл} не найден: заслон имени поля перестал что-либо охранять")
    плохие = [
        f"{файл}:{n}: {s.strip()}"
        for n, s in enumerate(строки(путь), 1)
        if re.search(r'"awaiting_since"\s*:', s)
    ]
    assert not плохие, (
        "в кадр уезжает сырое имя `awaiting_since`, которого экран не знает — "
        "шкала ожидания это поле не увидит:\n" + "\n".join(плохие)
    )


def test_screen_really_reads_waiting_since() -> None:
    """Вторая половина заслона: убеждаемся, что экран читает именно это имя.

    Без неё проверка выше охраняет догадку: перепиши фронт поле завтра — и она
    продолжит зеленеть, запрещая имя, которое как раз стало правильным.
    """
    фронт = КОРЕНЬ.parent / "frontend/src/shared/lib/waiting.ts"
    текст = фронт.read_text(encoding="utf-8")
    assert "row.waiting_since" in текст, (
        "шкала на экране больше не читает `waiting_since` — заслон имени поля устарел"
    )
    assert "awaiting_since" not in текст.replace("`app/services/conversation_status.py", ""), (
        "фронт начал знать про `awaiting_since` — правило имени пора пересмотреть"
    )
