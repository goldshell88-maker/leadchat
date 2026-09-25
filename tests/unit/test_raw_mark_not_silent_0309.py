"""Отметка в сыром журнале имеет право не сработать, но не имеет права молчать.

⚠ ПОЧЕМУ ЭТО ВООБЩЕ ВАЖНО. Колонку `webhook_raw_log.error` пишет ровно одна
функция — `_mark_raw`. По этой же колонке считает тревогу сторож «Авито
поменял формат» (`scheduler/jobs/watchdog.py::check_inbound_unparsed`:
`count(*) WHERE error IS NOT NULL`).

Всё тело `_mark_raw` стояло под `contextlib.suppress(Exception)`. Получался
заслон, который снимается ровно теми условиями, ради которых заведён: база
заикнулась — отметка не легла — счётчик остался нулевым — сторож доложил, что
всё хорошо. Ни строки в журнале, ни следа.
"""

from __future__ import annotations

import pytest

from app.workers import inbound

pytestmark = pytest.mark.anyio


class ПадающаяФабрика:
    def __call__(self):  # noqa: ANN204
        raise RuntimeError("база недоступна")


async def test_отказ_отметки_не_роняет_разбор_обращения(monkeypatch) -> None:
    """Проглатывание остаётся: обращение клиента важнее строки в журнале."""
    monkeypatch.setattr(inbound.log, "error", lambda *a, **k: None)
    await inbound._mark_raw({"db_session_factory": ПадающаяФабрика()}, "1-0", processed=True)


async def test_отказ_отметки_виден_в_журнале(monkeypatch) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: уходит молчание, а не проглатывание.

    Верни `contextlib.suppress` — и здесь станет пусто, а сторож разбора снова
    ослепнет без единого следа.
    """
    записи: list[tuple[str, dict]] = []

    def ловим(event: str, **kw: object) -> None:
        записи.append((event, dict(kw)))

    monkeypatch.setattr(inbound.log, "error", ловим)
    await inbound._mark_raw({"db_session_factory": ПадающаяФабрика()}, "1-0", error="boom")

    имена = [e for e, _ in записи]
    assert "webhook.raw_mark_failed" in имена, (
        "отказ отметки не оставил следа: сторож «Авито поменял формат» считает "
        "по колонке, которую эта функция пишет единственная"
    )
    _, поля = next(з for з in записи if з[0] == "webhook.raw_mark_failed")
    assert поля.get("stream_id") == "1-0", "без идентификатора запись бесполезна"
    assert поля.get("had_error") is True, "не видно, теряется ли отметка ОБ ОШИБКЕ"
