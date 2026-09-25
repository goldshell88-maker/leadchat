"""Ответ, сочинённый до нового сообщения клиента, отбрасывается (вопрос владельца 30.08).

«Можно ли сделать так, чтобы бот в моменте менял свой ответ?» — клиент спросил
цену и, пока модель думала, дописал адрес. Ответ по неполному контексту уходить
не должен: движок запоминает последнее входящее ДО вызова ИИ и сверяет ПОСЛЕ.
Появилось новое — ответ отбрасывается молча, шаг сценария не двигается: свежий
тик от нового сообщения (его вебхук уже в очереди) отвечает по полной картине.

С 24.09 это сторож пути ВЫЗОВА НА МЕСТЕ — песочницы и тестов движка. Тик бота
зовёт модель вне транзакции и сверяет свежесть во второй транзакции: его
сторожат поведением tests/unit/test_bot_two_phase_2409.py и
tests/integration/test_bot_two_phase_pg.py.
"""

import pathlib

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def _тело() -> str:
    текст = (КОРЕНЬ / "app" / "bots" / "engine.py").read_text(encoding="utf-8")
    i = текст.find("async def exec_ai_answer")
    j = текст.find("async def ", i + 10)
    return текст[i:j]


def test_маркер_свежести_вокруг_вызова() -> None:
    тело = _тело()
    assert "_вход_посл" in тело and "_выход_посл" in тело
    # порядок: вход-маркер ДО _call_ai_answer, выход-маркер ПОСЛЕ
    assert тело.index("_вход_посл") < тело.index("_call_ai_answer(context_messages)")
    assert тело.index("_call_ai_answer(context_messages)") < тело.index("_выход_посл")


def test_устаревший_ответ_отбрасывается_молча() -> None:
    тело = _тело()
    assert "reply_stale_dropped" in тело
    assert "ai_call_stale" in тело
    # отбрасываем ДО обработки needs_operator/отправки
    assert тело.index("reply_stale_dropped") < тело.index("needs_operator")


def test_шаг_не_двигается() -> None:
    """return без _goto: свежий тик проходит сценарий заново с полным контекстом."""
    тело = _тело()
    i = тело.index("reply_stale_dropped")
    хвост = тело[i : i + 400]
    assert "return" in хвост
    assert "_goto" not in хвост.split("return")[1][:120]
