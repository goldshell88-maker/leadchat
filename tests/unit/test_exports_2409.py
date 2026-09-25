"""Выгрузки: текст остаётся текстом, подписи — человеческие (проверка 24.09).

1. CSV писал ячейки как есть: Excel превращал телефон «+7…» в число 7,92E+10,
   а имя клиента «=…» — в формулу.
2. «Закрыт кем» у автозакрытий: машинное `system` или пусто, что читалось как
   «никто».
3. Лист «Сводка» печатал фильтры голыми UUID.
"""

from __future__ import annotations

import csv
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.services import stats as st
from app.services.csv_cells import csv_cell
from tests.unit.test_stats_export import _payload


@pytest.mark.parametrize(
    ("raw", "written"),
    [
        ("+79001112241", '="+79001112241"'),
        ("79001112241", '="79001112241"'),
        ("=1+1", "'=1+1"),
        (
            '=HYPERLINK("https://x.example/?"&D2;"Клиент")',
            '\'=HYPERLINK("https://x.example/?"&D2;"Клиент")',
        ),
        ("+ скидка", "'+ скидка"),
        ("-", "'-"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tимя", "'\tимя"),
        ("Ольга Никитина", "Ольга Никитина"),
        ("", ""),
        (42, 42),
        (None, None),
    ],
)
def test_csv_cell_keeps_text_text(raw: object, written: object) -> None:
    assert csv_cell(raw) == written


async def test_stats_csv_writes_phone_and_formula_as_text(tmp_path: Path) -> None:
    async def rows():  # noqa: ANN202 — асинхронный генератор строк для писателя
        yield ["=1+1", "+79001112241", 3]

    path = tmp_path / "out.csv"
    await st.write_csv(path, ["Клиент", "Телефон", "Сообщений"], rows())

    with path.open(encoding="utf-8-sig", newline="") as fh:
        body = list(csv.reader(fh, delimiter=";"))
    assert body[1] == ["'=1+1", '="+79001112241"', "3"]


@pytest.mark.parametrize(
    ("closed_by", "closed_at", "label"),
    [
        ("operator", "2026-09-24 10:00", "Оператор"),
        ("bot", "2026-09-24 10:00", "Бот"),
        ("system", "2026-09-24 10:00", "Автоматически"),
        # Сторож до 24.09 закрывал без ключа `by`.
        (None, "2026-09-24 10:00", "Автоматически"),
        (None, None, None),
        ("robot", "2026-09-24 10:00", "robot"),
    ],
)
def test_closed_by_is_printed_in_russian(
    closed_by: str | None, closed_at: str | None, label: str | None
) -> None:
    assert st.closed_by_label(closed_by, closed_at) == label


async def test_summary_sheet_names_the_channel_and_the_staff(
    db, make_user, make_avito_account
) -> None:
    account = await make_avito_account(title="Парт-7")
    anna = await make_user("anna@example.com", full_name="Анна Смирнова")
    ghost = uuid.uuid4()
    filters = st.Filters(account_id=account.id, manager_ids=(anna.id, ghost))

    names = await st.filter_names(db, filters)
    assert names.account == "Парт-7"
    assert names.managers == ("Анна Смирнова", f"неизвестный сотрудник ({ghost})")

    day = date(2026, 9, 1)
    pairs = st.summary_pairs(_payload(), st.Period(day, day), filters, names=names)
    rows = {row[0]: row[1] for row in pairs}
    assert rows["Аккаунт"] == "Парт-7"
    assert rows["Менеджеры"] == f"Анна Смирнова, неизвестный сотрудник ({ghost})"


async def test_failed_export_tells_the_person_in_words(redis) -> None:
    """Сбой воркера — фраза для человека, а не код `internal` (проверка 24.09)."""
    user_id = uuid.uuid4()
    job_id = uuid.uuid4().hex
    await redis.hset(
        st.export_status_key(job_id), mapping={"status": "pending", "user_id": str(user_id)}
    )

    def broken_factory() -> None:
        raise RuntimeError("база недоступна")

    params = {
        "user_id": str(user_id),
        "date_from": "2026-09-01",
        "date_to": "2026-09-02",
        "format": "csv",
    }
    with pytest.raises(RuntimeError):
        await st.export_stats(
            {"redis": redis, "db_session_factory": broken_factory}, job_id, params
        )

    status = await st.export_status(redis, job_id, user_id=user_id)
    assert status["status"] == "failed"
    assert status["error"] == st.EXPORT_FAILED_TEXT
