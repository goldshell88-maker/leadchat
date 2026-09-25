"""Ночные уборки файлов работают там, где им можно писать (проверка 24.09).

Выгрузки статистики старше семи суток (в них имена и телефоны клиентов) и
брошенные вложения удаляют задачи планировщика. Том вложений был смонтирован
планировщику `:ro`: каждое удаление получало EROFS, уборка писала
предупреждение на каждый файл и докладывала `removed=0` — с 05.08 не удалено ни
одного файла, и заметить это по журналу было нельзя.

Стережём две вещи: том планировщика в compose — на запись, и уборка, упёршись
в запрет записи, говорит об этом ошибкой один раз и не перебирает остальное.
"""

from __future__ import annotations

import errno
import os
import pathlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from structlog.testing import capture_logs

from app.services import media
from app.services import stats as st

ROOT = pathlib.Path(__file__).resolve().parents[2]
MEDIA = "/var/leadchat/media"


def _service_running(compose: dict[str, Any], module: str) -> dict[str, Any]:
    for service in compose["services"].values():
        if module in str(service.get("command", "")):
            return service
    raise AssertionError(f"в compose нет сервиса с {module}")


def test_the_scheduler_can_delete_in_the_media_folder() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    scheduler = _service_running(compose, "app.scheduler.main")

    mounts = [v for v in scheduler["volumes"] if str(v).split(":")[0] == MEDIA]

    assert mounts, "планировщику нужен том вложений: на нём уборки и замер диска"
    assert all(not str(v).endswith(":ro") for v in mounts), mounts


def _read_only(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    tried: list[pathlib.Path] = []

    def unlink(self: pathlib.Path, missing_ok: bool = False) -> None:
        tried.append(self)
        raise OSError(errno.EROFS, "Read-only file system", str(self))

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    return tried


def test_export_cleanup_on_a_read_only_folder_says_so_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    directory = st.export_dir()
    directory.mkdir(parents=True)
    old = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    for name in ("a.csv", "b.csv", "c.csv"):
        path = directory / name
        path.write_text("x")
        os.utime(path, (old, old))
    tried = _read_only(monkeypatch)

    with capture_logs() as logs:
        removed = st.cleanup_export_files()

    assert removed == 0
    assert len(tried) == 1, "дальше перебирать незачем: запрет записи на весь том"
    assert [e["event"] for e in logs if e["log_level"] == "error"] == [
        "stats.export_cleanup_readonly"
    ]


async def test_orphan_cleanup_on_a_read_only_folder_says_so_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(media.settings, "media_root", str(tmp_path))

    class _NoRows:
        def __iter__(self):
            return iter(())

    class _StubDb:
        async def execute(self, *args, **kwargs):
            return _NoRows()

    long_ago = time.time() - 5 * 24 * 3600
    for n in range(3):
        orphan = tmp_path / "2026" / "07" / "5f" / f"5f6a3c2e-0000-4000-8000-00000000000{n}.png"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"png")
        os.utime(orphan, (long_ago, long_ago))
    tried = _read_only(monkeypatch)

    with capture_logs() as logs:
        removed = await media.collect_orphans(_StubDb())  # type: ignore[arg-type]

    assert removed == 0
    assert len(tried) == 1
    assert [e["event"] for e in logs if e["log_level"] == "error"] == [
        "media.orphan_cleanup_readonly"
    ]
