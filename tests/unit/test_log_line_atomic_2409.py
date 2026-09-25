"""Каждая запись журнала уходит одним write() (проверка 24.09).

У api два процесса uvicorn на одном stdout. PrintLogger structlog писал текст
и перевод строки двумя вызовами, записи процессов перемежались («A B\\n\\n»), и
разбор журнала по строкам терял одну из двух: 1 503 склеенные строки в
суточном архиве api.
"""

from __future__ import annotations

import json
import sys

import pytest
import structlog

from app.core.logging import configure_logging


class _Writes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, text: str) -> int:
        self.calls.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def test_a_record_and_its_newline_leave_in_one_write(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _Writes()
    before = structlog.get_config()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("LOG_CACHE", "0")
    configure_logging("api")
    try:
        structlog.get_logger("app.test").info("probe.line", n=1)
    finally:
        structlog.configure(**before)

    records = [c for c in out.calls if "probe.line" in c]
    assert len(records) == 1
    assert records[0].endswith("\n")
    assert json.loads(records[0])["event"] == "probe.line"
