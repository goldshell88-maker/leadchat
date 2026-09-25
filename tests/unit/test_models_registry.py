"""Каждая модель зарегистрирована в `app/models/__init__` (FUNC-51).

ЧТО СЛУЧИЛОСЬ. `notifications` и `notification_reads` не были перечислены в
пакете моделей со дня их появления (миграция 0006). Приложение работало:
центр уведомлений импортирует `app.models.notification` сам, и таблицы у него
на месте. Молчала только автогенерация миграций — а она сравнивает базу с
`Base.metadata`, куда попадает ровно то, что импортировал `app/models/__init__`.
Незарегистрированная таблица для неё лишняя, и в следующую же миграцию по
любому поводу уехало бы `op.drop_table("notifications")`: центр уведомлений
вместе с историей поломок за 90 дней стирается одной строкой, которую в
чужой миграции не замечают.

ПОЧЕМУ ПРОВЕРКА В ОТДЕЛЬНОМ ПРОЦЕССЕ. `Base.metadata` — глобальное состояние
на весь прогон: к моменту этого теста соседние тесты уже импортировали
половину моделей напрямую, и сравнение «до и после» в этом же процессе
показало бы, что всё на месте. То есть тест зеленел бы именно в том случае,
ради которого написан. Чистый процесс — единственный способ спросить честно.

ПОЧЕМУ НЕ СВЕРКА С МИГРАЦИЯМИ. Проверять «таблицы метаданных == таблицы в
базе» соблазнительно, но это другой вопрос и другой ответ: в базе законно
живут вещи без модели (партиции `messages`, материализованные виды
статистики). Здесь спрашивается ровно одно — не потерялся ли импорт.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Пакет импортируется первым, затем — каждый его модуль по отдельности. Всё,
# что появилось в метаданных на втором шаге, в `__init__` не перечислено.
PROBE = """
import importlib
import pkgutil

import app.models as pkg
from app.models.base import Base

registered = set(Base.metadata.tables)
for module in pkgutil.iter_modules(pkg.__path__):
    importlib.import_module(f"app.models.{module.name}")
print(",".join(sorted(set(Base.metadata.tables) - registered)))
"""


def test_every_model_is_imported_by_the_models_package() -> None:
    probe = subprocess.run(  # noqa: S603 — свой же интерпретатор и свой же текст
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr

    forgotten = [name for name in probe.stdout.strip().split(",") if name]
    assert not forgotten, (
        "эти таблицы есть в app/models, но не импортированы в app/models/__init__.py: "
        f"{forgotten}. Для alembic --autogenerate они лишние, и он предложит их УДАЛИТЬ — "
        "добавьте импорт и строку в __all__"
    )
