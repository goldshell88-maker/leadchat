"""Офсайт-копии уходят из backup.sh только зашифрованными.

Гоняется настоящий скрипт с настоящим age; docker и rclone подставные:
«бакет» — каталог рядом. Проверяется то, что увидел бы посторонний с доступом
к хранилищу: ни одного открытого дампа, ни одного байта переписки в копиях.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash"),
    pytest.mark.skipif(
        shutil.which("age") is None or shutil.which("age-keygen") is None, reason="нужен age"
    ),
]

MARKER = "+79001112240-client-marker"

FAKE_DOCKER = r"""
case "$*" in
  *pg_dump*)
    name="${@: -1}"; dump="$BACKUP_DIR/${name#/backups/}"
    { printf 'PGDMP'; printf '%s' "$MARKER"; head -c 30000 /dev/zero; } > "$dump" ;;
  *"pg_restore --list"*)
    for i in $(seq 1 60); do echo "$i; 0 0 TABLE DATA public filler_$i u"; done
    for t in users avito_accounts conversations messages; do
      echo "99; 0 0 TABLE DATA public $t u"
    done ;;
esac
"""

# rclone copy <файл> offsite:bucket/<каталог>/ — кладёт файл в BUCKET/<каталог>/.
FAKE_RCLONE = r"""
case "$1" in
  copy) dest="$BUCKET/${3#*:*/}"; mkdir -p "$dest"; cp "$2" "$dest" ;;
  *) : ;;
esac
"""


def _stub(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _identity(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"
    public = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout
    recipients.write_text(public, encoding="utf-8")
    return key, recipients


def _run_backup(
    tmp_path: pathlib.Path, recipients: pathlib.Path | None
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "docker", FAKE_DOCKER)
    _stub(bin_dir / "rclone", FAKE_RCLONE)

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=d\n", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    media = tmp_path / "media" / "2026" / "09"
    media.mkdir(parents=True)
    (media / "photo.jpg").write_text(MARKER, encoding="utf-8")
    bucket = tmp_path / "bucket"

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_DIR=str(deploy),
        COMPOSE_FILE="compose.yml",
        BACKUP_DIR=str(backups),
        MEDIA_ROOT=str(tmp_path / "media"),
        RCLONE_REMOTE="offsite:bucket",
        BACKUP_AGE_RECIPIENTS=str(recipients or tmp_path / "нет-ключа.txt"),
        BUCKET=str(bucket),
        MARKER=MARKER,
        # bsdtar на macOS иначе добавляет в архив служебные ._-файлы; GNU tar
        # на сервере их не пишет.
        COPYFILE_DISABLE="1",
    )
    for name in ("INTERNAL_SERVICE_TOKEN", "BACKUP_LABEL", "RCLONE_EXTRA_ARGS"):
        env.pop(name, None)
    proc = subprocess.run(
        ["bash", str(ROOT / "deploy" / "backup.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc, backups, bucket


def _decrypt(path: pathlib.Path, key: pathlib.Path) -> bytes:
    return subprocess.run(
        ["age", "-d", "-i", str(key), str(path)], check=True, capture_output=True
    ).stdout


def test_the_bucket_gets_an_encrypted_dump_that_decrypts_to_the_original(
    tmp_path: pathlib.Path,
) -> None:
    key, recipients = _identity(tmp_path)

    proc, backups, bucket = _run_backup(tmp_path, recipients)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    uploaded = sorted(p.relative_to(bucket).as_posix() for p in bucket.rglob("*") if p.is_file())
    assert len(uploaded) == 2, uploaded
    assert uploaded[0].startswith("db/db_") and uploaded[0].endswith(".dump.age")
    assert uploaded[1].startswith("media-archive/media_") and uploaded[1].endswith(".tar.age")

    for rel in uploaded:
        assert MARKER.encode() not in (bucket / rel).read_bytes(), f"{rel} лежит в открытом виде"

    (local,) = backups.glob("db_*.dump")
    assert _decrypt(bucket / uploaded[0], key) == local.read_bytes()


def test_the_media_archive_holds_every_attachment(tmp_path: pathlib.Path) -> None:
    key, recipients = _identity(tmp_path)

    proc, _, bucket = _run_backup(tmp_path, recipients)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    (archive,) = (bucket / "media-archive").glob("media_*.tar.age")
    plain = tmp_path / "media.tar"
    plain.write_bytes(_decrypt(archive, key))
    with tarfile.open(plain) as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
    assert names == ["./2026/09/photo.jpg"]


def test_without_the_key_no_copy_leaves_the_server(tmp_path: pathlib.Path) -> None:
    """Нет ключа — нет офсайта; открытый дамп наружу не уходит «на время»."""
    proc, backups, bucket = _run_backup(tmp_path, recipients=None)

    assert proc.returncode != 0
    assert "нет ключа шифрования" in proc.stdout
    assert not bucket.exists()
    assert list(backups.glob("db_*.dump")), "локальный дамп обязан остаться"
