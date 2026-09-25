"""Заготовка сценария говорит голосом частного мастера и без эмодзи.

ПОЧЕМУ ТЕСТ. Запрет эмодзи — решение владельца от 30.07 (замер 14 000 живых реплик:
мастера их практически не пишут, а эмодзи в каждом сообщении выдаёт бота). Легенда —
частный мастер, работающий САМ: «Это сервис Lead Partner», «перезвоним первыми»,
«мастер ответит» ломают её первой же строкой. В JSON комментария не поставить,
поэтому правило живёт здесь.

⚠ ПРОВЕРЯЕМ ТОЛЬКО ТЕКСТЫ, УХОДЯЩИЕ КЛИЕНТУ. Заметка `note_contact` видна одной
смене, и «🤖» в ней — метка происхождения, а не обращение к человеку.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

from app.bots.scenarios import default_scenario

МИГРАЦИЯ = (
    pathlib.Path(__file__).resolve().parents[2]
    / "app/db/migrations/versions/0052_scenario_legend_repair.py"
)

ЭМОДЗИ = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍←-⇿✔✖✅❌]")
ЧУЖОЙ_ГОЛОС = re.compile(
    r"lead\s*partner|наш\s+(?:мастер|специалист)|мастер\s+(?:ответит|перезвонит|свяжется)"
    r"|перезвон(?:им|ит)\s+перв|наш[аи]\s+компани|передам\s+коллег",
    re.IGNORECASE,
)


def _клиентские_тексты():
    out = []
    for шаг in default_scenario()["steps"]:
        if шаг.get("type") == "note":
            continue
        p = шаг.get("params") or {}
        for ключ in ("text", "retry_text"):
            if isinstance(p.get(ключ), str):
                out.append((шаг["id"], ключ, p[ключ]))
    return out


def test_в_заготовке_нет_эмодзи():
    плохие = [(i, k, t) for i, k, t in _клиентские_тексты() if ЭМОДЗИ.search(t)]
    assert not плохие, плохие


def test_заготовка_говорит_от_первого_лица():
    плохие = [(i, k, t) for i, k, t in _клиентские_тексты() if ЧУЖОЙ_ГОЛОС.search(t)]
    assert not плохие, плохие


def test_миграция_чинит_боевой_литерал():
    """Ровно та строка, что владелец прислал со снимка 26.08."""
    spec = importlib.util.spec_from_file_location("m0052", МИГРАЦИЯ)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    было = {
        "steps": [
            {
                "id": "ping_problem",
                "params": {"text": "Ну что, подскажете? Сразу сориентирую по мастеру и времени"},
            }
        ]
    }
    стало, n = mod._починить(было)
    assert n == 1
    assert стало["steps"][0]["params"]["text"] == "Ну что, подскажете? Сразу сориентирую по времени"
    # чужой текст миграция не трогает
    свой = {"steps": [{"params": {"text": "Напишу вам утром"}}]}
    assert mod._починить(свой) == (свой, 0)
