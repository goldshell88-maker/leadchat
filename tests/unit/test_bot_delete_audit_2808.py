"""Удаление сценария бота — своё событие журнала, а не «обновление».

⚠ УДАЛЕНИЕ БОТА НЕОБРАТИМО: мягкого удаления у ботов нет, в отличие от
сотрудников. А записывалось оно как `bot.updated` с признаком в подробностях —
то есть в журнале выглядело ровно как десяток обычных сохранений сценария и как
включения-выключения, которые пишутся тем же действием.

Через месяц на вопрос «куда делся сценарий» журнал отвечал «Обновлён сценарий
бота», и разобрать, что именно тогда произошло, можно было только раскрыв JSON
каждой строки за тот день.
"""

from __future__ import annotations

import pytest

from app.services import audit as audit_svc

pytestmark = pytest.mark.anyio


def test_у_удаления_своя_подпись() -> None:
    assert audit_svc.AUDIT_ACTIONS["bot.deleted"] == "Удалён сценарий бота"
    assert audit_svc.AUDIT_ACTIONS["bot.updated"] != audit_svc.AUDIT_ACTIONS["bot.deleted"], (
        "удаление и обновление снова неразличимы в журнале"
    )


def test_ручка_пишет_именно_его() -> None:
    """Сторож проводки: подпись без вызова — это подпись в никуда."""
    import inspect

    from app.api.routes import bots as bots_routes

    источник = inspect.getsource(bots_routes.delete_bot)
    assert 'action="bot.deleted"' in источник, (
        "удаление снова пишется как обновление — в журнале его не отличить"
    )
    assert 'action="bot.updated"' not in источник
