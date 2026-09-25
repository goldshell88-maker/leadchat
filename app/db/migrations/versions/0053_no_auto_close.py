"""Молчание клиента больше не закрывает диалог само: ветка таймаута ведёт к менеджеру.

РЕШЕНИЕ ВЛАДЕЛЬЦА, ЗАПИСАННОЕ ЕЩЁ В ЗАГОТОВКЕ (`app/bots/scenarios/__init__.py`):
«автоматического закрытия по таймауту НЕТ — диалог живёт, пока менеджер не закроет его
руками». В заготовке так и сделано: ветка молчания ведёт на `handoff_no_reply`.
А в боевом сценарии, собранном в панели, последним шагом стоит `close_silent` типа
`close`: сутки тишины — и диалог закрыт.

ЧТО ГОВОРЯТ ДАННЫЕ (боевая база на 26.08, 2566 смен статуса):
    оператор  новый → закрыт      1592
    СИСТЕМА   закрыт → НОВЫЙ       517   ← клиент написал в закрытый диалог
    бот       новый → закрыт          4
Автозакрытие ботом сработало ЧЕТЫРЕ раза за всё время — то есть выключение ничего не
ломает. А 517 переоткрытий показывают цену ошибки с другой стороны: закрытый диалог
клиента не останавливает, он пишет дальше, и его приходится поднимать обратно.

⚠ ПЕРЕПИСЫВАЕМ ССЫЛКУ, А НЕ УДАЛЯЕМ ШАГ. Шаг `close` остаётся в сценарии: админ вправе
собрать им свою ветку осознанно. Меняется только то, куда ведёт ТАЙМАУТ ожидания —
теперь к человеку, как в заготовке.

⚠ ТРОГАЕМ ТОЛЬКО МОЛЧАЛИВОЕ ЗАКРЫТИЕ. `close` с текстом — это прощание, которое админ
написал сам, и оно не про молчание клиента.

ОБРАТНОЙ ПРАВКИ НЕТ: возвращать автозакрытие, которое владелец отменил, незачем.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

ПЕРЕДАЧА = {
    "id": "handoff_no_reply",
    "type": "handoff",
    "params": {
        "reason": "ask_timeout",
        "comment": (
            "Клиент не ответил. Диалог остаётся в общей очереди — "
            "автозакрытия по таймауту нет, закрывает менеджер вручную"
        ),
        "tags": ["без ответа"],
    },
}


def _починить(сценарий: dict[str, Any]) -> int:
    шаги = сценарий.get("steps")
    if not isinstance(шаги, list):
        return 0
    молчаливые = {
        s.get("id")
        for s in шаги
        if isinstance(s, dict)
        and s.get("type") == "close"
        and not ((s.get("params") or {}).get("text") or "").strip()
    }
    if not молчаливые:
        return 0
    правок = 0
    for s in шаги:
        if isinstance(s, dict) and s.get("on_timeout") in молчаливые:
            s["on_timeout"] = ПЕРЕДАЧА["id"]
            правок += 1
    if not правок:
        return 0
    if not any(isinstance(s, dict) and s.get("id") == ПЕРЕДАЧА["id"] for s in шаги):
        шаги.append(json.loads(json.dumps(ПЕРЕДАЧА)))
    return правок


def upgrade() -> None:
    conn = op.get_bind()
    for bid, сценарий in conn.execute(sa.text("SELECT id, scenario FROM bots")).fetchall():
        if сценарий is None:
            continue
        данные = json.loads(сценарий) if isinstance(сценарий, str) else сценарий
        if not isinstance(данные, dict):
            continue
        n = _починить(данные)
        if not n:
            continue
        conn.execute(
            sa.text("UPDATE bots SET scenario = CAST(:s AS jsonb) WHERE id = :i"),
            {"s": json.dumps(данные, ensure_ascii=False), "i": bid},
        )
        print(f"0053: бот {bid} — веток таймаута переведено к человеку: {n}")


def downgrade() -> None:
    """Намеренно пусто: см. шапку."""
