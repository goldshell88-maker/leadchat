"""Выгрузка «Разбора диалогов» — фоном, с квотой и журналом (проверка 24.09).

Было: CSV в запросе с потолком 10 000 строк (меньше любого готового периода —
на бою «30 дней» это 44 052 строки), без записи в журнал и без квоты, хотя
в файле имена и телефоны клиентов. Стало: POST ставит задачу, воркер пишет
файл, человек скачивает его по подписанной ссылке, выгрузка — строка журнала
`dialogs.exported`, слот и суточная квота общие с выгрузкой статистики.
"""

from __future__ import annotations

import csv
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.services import conversation_table as table
from app.services import dialogs_export
from app.services import stats as st


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    return tmp_path


async def _run_job(redis, db_sessionmaker, *, user_id: uuid.UUID, **filters) -> str:
    job_id = uuid.uuid4().hex
    await st.reserve_export_slot(redis, user_id, job_id)
    await redis.hset(
        st.export_status_key(job_id), mapping={"status": "pending", "user_id": str(user_id)}
    )
    params = {
        "user_id": str(user_id),
        "filters": dialogs_export.filters_to_params(table.TableFilters(**filters)),
        "sort": table.DEFAULT_SORT,
        "direction": "desc",
    }
    await dialogs_export.export_dialogs(
        {"redis": redis, "db_session_factory": db_sessionmaker}, job_id, params
    )
    return job_id


async def test_the_job_writes_the_file_the_status_and_the_journal(
    redis, db_sessionmaker, seed_conversation, make_user, media
) -> None:
    head = await make_user("head@example.com", role="head")
    job_id = await _run_job(
        redis, db_sessionmaker, user_id=head.id, account_id=seed_conversation.account.id
    )

    status = await st.export_status(redis, job_id, user_id=head.id)
    assert status["status"] == "done"
    assert status["rows"] == 1
    assert status["url"], "файл отдаётся подписанной ссылкой"

    files = list((media / st.EXPORT_DIRNAME).glob("leadchat-dialogs_*.csv"))
    assert len(files) == 1
    assert files[0].read_bytes().startswith(b"\xef\xbb\xbf"), "без BOM русский Excel читает cp1251"
    with files[0].open(encoding="utf-8-sig", newline="") as fh:
        body = list(csv.reader(fh, delimiter=";"))
    assert body[0] == list(table.CSV_HEADER)
    assert body[1][0] == "Иван Петров"

    async with db_sessionmaker() as s:
        row = (
            await s.execute(select(AuditLog).where(AuditLog.action == "dialogs.exported"))
        ).scalar_one()
    assert row.user_id == head.id
    assert row.details["rows"] == 1
    assert row.details["account_id"] == str(seed_conversation.account.id)

    # Слот освобождён: следующая выгрузка этого человека не упрётся в 409.
    assert not await redis.exists(st.export_active_key(head.id))


async def test_the_job_refuses_in_words_above_the_ceiling(
    redis, db_sessionmaker, seed_conversation, make_user, media, monkeypatch
) -> None:
    monkeypatch.setattr(table, "EXPORT_MAX_ROWS", 0)
    head = await make_user("head@example.com", role="head")

    job_id = await _run_job(redis, db_sessionmaker, user_id=head.id)

    status = await st.export_status(redis, job_id, user_id=head.id)
    assert status["status"] == "failed"
    assert status["error"].startswith("Слишком много строк (1)")
    assert not list((media / st.EXPORT_DIRNAME).glob("*.csv")), "обрезанный файл не оставляем"


def test_filters_survive_the_queue() -> None:
    filters = table.TableFilters(
        status="closed",
        account_id=uuid.uuid4(),
        q="Иван",
        bot_active=False,
    )
    params = dialogs_export.filters_to_params(filters)
    assert dialogs_export.filters_from_params(params) == filters


async def test_start_answers_202_and_shares_the_slot_with_stats(
    client, tokens, seed_conversation, redis
) -> None:
    r = await client.post(
        "/api/v1/conversations/table/export",
        params={"status": "new"},
        headers=auth(tokens, "manager"),
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert await redis.exists(f"arq:job:{job_id}")

    again = await client.post("/api/v1/conversations/table/export", headers=auth(tokens, "manager"))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "export_already_running"


async def test_status_is_visible_only_to_its_author(client, tokens, seed_conversation) -> None:
    r = await client.post("/api/v1/conversations/table/export", headers=auth(tokens, "manager"))
    job_id = r.json()["job_id"]

    own = await client.get(
        f"/api/v1/conversations/table/export/{job_id}", headers=auth(tokens, "manager")
    )
    assert own.status_code == 200
    assert own.json()["status"] == "pending"

    foreign = await client.get(
        f"/api/v1/conversations/table/export/{job_id}", headers=auth(tokens, "head")
    )
    assert foreign.status_code == 404


async def test_the_old_address_explains_itself(client, tokens) -> None:
    r = await client.get("/api/v1/conversations/table/export", headers=auth(tokens, "manager"))
    assert r.status_code == 410
    assert "обновите страницу" in r.json()["error"]["message"]


def test_the_worker_runs_the_job() -> None:
    from app.workers.main import WorkerSettings

    names = {getattr(f, "__name__", "") for f in WorkerSettings.functions}
    assert dialogs_export.EXPORT_JOB in names
