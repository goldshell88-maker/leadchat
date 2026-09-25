"""SM-10 судит только каналы, выпавшие в needs_reauth ПОСЛЕ выкатки (24.09).

24.09 в 14:10 UTC Авито перестал принимать токен канала «Степан КП», и
следующая выкатка откатилась на SM-10, хотя обновление токенов она не
трогала. Вернуть такой канал может только владелец — переподключением.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIFF = ROOT / "deploy/reauth_diff.py"
SHIP = ROOT / "deploy/workstation/ship.sh"
SMOKE = ROOT / "deploy/smoke.sh"

A, B, C = (
    "3f1c1a2e-0000-4000-8000-00000000000a",
    "3f1c1a2e-0000-4000-8000-00000000000b",
    "3f1c1a2e-0000-4000-8000-00000000000c",
)


def _accounts(*reauth: str, active: tuple[str, ...] = (C,)) -> str:
    items = [{"id": a, "status": "needs_reauth"} for a in reauth]
    items += [{"id": a, "status": "active"} for a in active]
    return json.dumps({"items": items, "page": {"limit": 50, "offset": 0, "total": len(items)}})


def _run(body: str, before: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DIFF), before],
        input=body,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("reauth", "before", "printed"),
    [
        ((), "", "0 0"),
        # Канал выпал до выкатки — прежний, выкатку не роняет.
        ((A,), A, "0 1"),
        # Выпал новый рядом с прежним — регресс.
        ((A, B), A, "1 1"),
        # Прежний вернулся, выпал другой: число то же, но это регресс.
        ((B,), A, "1 0"),
        # Снимка нет (запуск руками) — судим по любому каналу, как раньше.
        ((A,), "", "1 0"),
    ],
)
def test_new_and_earlier_reauth_are_told_apart(
    reauth: tuple[str, ...], before: str, printed: str
) -> None:
    done = _run(_accounts(*reauth), before)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == printed


def test_unreadable_answer_is_an_error_not_a_pass() -> None:
    assert _run("<html>502</html>", "").returncode == 2


def test_ship_takes_the_snapshot_before_the_build_and_hands_it_to_smoke() -> None:
    ship = SHIP.read_text(encoding="utf-8")
    snapshot = ship.index('REAUTH_BEFORE="$(on_server')
    assert snapshot < ship.index('say "3. Сборка'), "снимок обязан сниматься до выкатки"
    assert "SMOKE_REAUTH_BEFORE='${REAUTH_BEFORE}' bash deploy/smoke.sh" in ship
    assert "python3 deploy/reauth_diff.py" in SMOKE.read_text(encoding="utf-8")
