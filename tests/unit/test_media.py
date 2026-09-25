"""Вложения — 01 §6.5, 05 §3.3 (07 §1.1).

Покрыто: разбор multipart без python-multipart, определение типа по
magic-байтам (а не по Content-Type), лимит размера, раскладка
``{yyyy}/{mm}/{2hex}/{uuid}.{ext}``, подписанные ссылки (валидная /
подделанная / истёкшая), RBAC и прикрепление media_id к сообщению.
"""

import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import settings
from app.services import media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64
ELF = b"\x7fELF" + b"\x00" * 64  # «картинка», которая на самом деле бинарник


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    """MEDIA_ROOT в tmp: в тестах никто не пишет в /var/leadchat/media."""
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    monkeypatch.setattr(settings, "media_sign_key", "unit-test-sign-key")
    return tmp_path


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Роутеры /media и /messages монтирует фабрика (app/main.py)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


def query_param(url: str, name: str) -> str:
    """Значение query-параметра подписанной ссылки."""
    match = re.search(rf"{name}=([^&]+)", url)
    assert match, f"в {url} нет параметра {name}"
    return match.group(1)


async def upload(api, tokens, data: bytes, name="price.png", ctype="image/png", role="manager"):
    return await api.post(
        "/api/v1/media", files={"file": (name, data, ctype)}, headers=auth(tokens, role)
    )


# --------------------------------------------------------- парсер multipart


def test_parse_multipart_extracts_file_part():
    """Тело, собранное httpx, разбирается без python-multipart."""
    request = httpx.Request("POST", "https://x/", files={"file": ("прайс.png", PNG, "image/png")})
    parsed = media.parse_multipart_file(request.read(), request.headers["content-type"])
    assert parsed.filename == "прайс.png"
    assert parsed.content_type == "image/png"
    assert parsed.data == PNG


def test_parse_multipart_ignores_other_fields():
    request = httpx.Request(
        "POST",
        "https://x/",
        data={"comment": "не файл"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    parsed = media.parse_multipart_file(request.read(), request.headers["content-type"])
    assert parsed.data == PDF


def test_parse_multipart_without_file_is_400():
    from app.core.errors import ApiError

    request = httpx.Request("POST", "https://x/", data={"comment": "без файла"})
    with pytest.raises(ApiError) as exc:
        media.parse_multipart_file(request.read(), request.headers["content-type"])
    assert exc.value.status == 400


def test_parse_multipart_requires_boundary():
    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        media.parse_multipart_file(b"whatever", "application/json")


# ------------------------------------------------------------ magic-байты


@pytest.mark.parametrize(
    "data,mime,ext,kind",
    [
        (PNG, "image/png", "png", "image"),
        (JPEG, "image/jpeg", "jpg", "image"),
        (WEBP, "image/webp", "webp", "image"),
        (PDF, "application/pdf", "pdf", "file"),
    ],
)
def test_sniff_recognizes_allowed_types(data, mime, ext, kind):
    assert media.sniff(data) == (mime, ext, kind)


@pytest.mark.parametrize("data", [ELF, b"GIF89a" + b"\x00" * 32, b"just text"])
def test_sniff_rejects_everything_else(data):
    assert media.sniff(data) is None


async def test_upload_trusts_magic_bytes_not_content_type(api, tokens):
    """07 §5: .exe под видом image/png должен получить 415, а не 201."""
    r = await upload(api, tokens, ELF, name="payload.png", ctype="image/png")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"


async def test_upload_rejects_oversize(api, tokens, monkeypatch):
    monkeypatch.setattr(settings, "media_max_size_mb", 1)
    r = await upload(api, tokens, PNG[:8] + b"\x00" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


async def test_upload_rejects_empty_file(api, tokens):
    r = await upload(api, tokens, b"")
    assert r.status_code == 400


# --------------------------------------------------------------- раскладка


async def test_upload_stores_file_in_dated_fanout_layout(api, tokens, media_dir, redis):
    """05 §3.3: {yyyy}/{mm}/{2hex}/{uuid}.{ext}."""
    r = await upload(api, tokens, PNG, name="прайс.png")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["kind"] == "image"
    assert out["name"] == "прайс.png"
    assert out["size"] == len(PNG)
    assert re.match(r"^m_[0-9a-f]{32}$", out["media_id"])

    relpath = out["url"].split("?")[0].removeprefix(media.MEDIA_URL_PREFIX + "/")
    assert media.MEDIA_RELPATH_RE.match(relpath), relpath
    year, month, fanout, filename = relpath.split("/")
    assert filename.startswith(fanout)  # fan-out по первым двум hex uuid
    stored = media_dir / relpath
    assert stored.is_file() and stored.read_bytes() == PNG
    assert not list(media_dir.rglob("*.part"))  # временный файл убран

    meta = await media.resolve_media(redis, out["media_id"])
    assert meta is not None
    assert meta["path"] == relpath and meta["mime"] == "image/png"


def test_build_relpath_uses_uuid_prefix():
    file_uuid = uuid.UUID("5f6a3c2e-0000-4000-8000-000000000001")
    relpath = media.build_relpath(file_uuid, "jpg")
    assert relpath.endswith(f"/5f/{file_uuid}.jpg")


# ----------------------------------------------------------------- подпись


def test_signed_url_roundtrip():
    relpath = "2026/08/5f/5f6a3c2e-0000-4000-8000-000000000001.jpg"
    url = media.signed_media_url(relpath)
    media.check_signature(relpath, query_param(url, "sig"), query_param(url, "exp"))


def test_tampered_signature_is_403():
    from app.core.errors import ApiError

    relpath = "2026/08/5f/5f6a3c2e-0000-4000-8000-000000000001.jpg"
    with pytest.raises(ApiError) as exc:
        media.check_signature(relpath, "definitely-not-the-signature", int(time.time()) + 60)
    assert exc.value.status == 403


def test_expired_signature_is_410():
    from app.core.errors import ApiError

    relpath = "2026/08/5f/5f6a3c2e-0000-4000-8000-000000000001.jpg"
    url = media.signed_media_url(relpath, ttl=-10)
    with pytest.raises(ApiError) as exc:
        media.check_signature(relpath, query_param(url, "sig"), query_param(url, "exp"))
    assert exc.value.status == 410


async def test_download_requires_valid_signature(api, tokens):
    url = (await upload(api, tokens, JPEG, name="a.jpg", ctype="image/jpeg")).json()["url"]
    ok = await api.get(url)
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/jpeg")
    assert ok.content == JPEG

    path = url.split("?")[0]
    assert (await api.get(path)).status_code == 403  # без подписи
    assert (await api.get(f"{path}?sig=bad&exp={int(time.time()) + 60}")).status_code == 403


async def test_download_rejects_path_traversal(api):
    r = await api.get("/api/v1/media/../../etc/passwd?sig=x&exp=1")
    assert r.status_code in (403, 404)


async def test_a_signature_with_cyrillic_is_refused_not_five_hundred(api, tokens):
    """Кириллица в подписи — отказ, а не «Сбой на нашей стороне».

    ЧТО БЫЛО. Подпись сравнивалась строками: `hmac.compare_digest` на строках
    бросает TypeError, если хоть в одной есть символ вне ASCII. Ссылка,
    прошедшая через почтовый клиент или скопированная с переносом, роняла
    ручку в 500 с трейсом в логе — вместо честного «ссылка не действует».
    Проверено на живом стенде: 500.

    Наша подпись — base64url, то есть заведомо ASCII: если она не кодируется,
    значит она не наша, и это ровно тот же отказ, что у подделки.
    """
    url = (await upload(api, tokens, PNG)).json()["url"]
    path = url.split("?")[0]

    r = await api.get(f"{path}?sig=подделка&exp={int(time.time()) + 60}")

    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "media_link_invalid"


# --------------------------------------------- выгрузки статистики (06 §4.5)


def test_export_relpath_is_servable():
    """Имя файла выгрузки собирает stats.export_filename, а раздаёт его тот
    же alias /api/v1/media — регулярка обязана его пропускать."""
    from app.services import stats as st

    period = st.Period(date(2026, 7, 1), date(2026, 7, 31))
    for fmt in st.EXPORT_FORMATS:
        relpath = f"{st.EXPORT_DIRNAME}/{st.export_filename(period, fmt, uuid.uuid4().hex)}"
        assert media.MEDIA_RELPATH_RE.match(relpath), relpath


def test_export_dirname_matches_stats():
    """Имя каталога выгрузок знают ДВА модуля, и знать они обязаны одно.

    `media.FOREIGN_DIRNAMES` не может импортировать `stats.EXPORT_DIRNAME`:
    статистика сама импортирует `media.signed_media_url`, вышло бы кольцо.
    Значит имя продублировано — и держать копии вместе должен тест.
    """
    from app.services import stats as st

    assert st.EXPORT_DIRNAME in media.FOREIGN_DIRNAMES


async def test_orphan_cleanup_keeps_statistics_exports(media_dir):
    """Уборка вложений не трогает выгрузки статистики.

    ЧТО БЫЛО. Выгрузки лежат в `MEDIA_ROOT/exports`, и на них не ссылается ни
    одно сообщение — для уборки вложений они выглядели брошенными файлами и
    удалялись через двое суток. При этом у выгрузок СВОЙ срок хранения, семь
    суток (06 §5.4, `stats.cleanup_export_files`), и своё задание планировщика.
    Два задания ходили по одному каталогу с разницей в полчаса, и более злое
    молча побеждало: обещанные семь дней на деле были двумя, а руководитель,
    вернувшийся к своей выгрузке в понедельник, находил мёртвую ссылку.

    База здесь подменена заглушкой намеренно: проверяется обход КАТАЛОГА, а не
    запрос к сообщениям. Настоящий запрос (он на PostgreSQL-функциях) покрыт
    интеграционным тестом `tests/integration/test_media_orphans_pg.py`.
    """

    class _NoRows:
        def __iter__(self):
            return iter(())

    class _StubDb:
        async def execute(self, *args, **kwargs):
            return _NoRows()

    long_ago = time.time() - 5 * 24 * 3600

    export = media_dir / "exports" / "leadchat-stats_2026-07-01_2026-07-31_deadbeef.csv"
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_bytes(b"data")
    os.utime(export, (long_ago, long_ago))

    orphan = media_dir / "2026" / "07" / "5f" / "5f6a3c2e-0000-4000-8000-000000000001.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(PNG)
    os.utime(orphan, (long_ago, long_ago))

    removed = await media.collect_orphans(_StubDb())

    assert export.is_file(), "выгрузка статистики съедена уборкой вложений"
    assert not orphan.exists(), "брошенное вложение обязано убираться"
    assert removed == 1


@pytest.mark.parametrize(
    "relpath",
    [
        "exports/../2026/08/5f/notes.txt",
        "exports/leadchat-stats_2026-07-01_2026-07-31_deadbeef.exe",
        "exports/anything-else.csv",
        "exports/",
    ],
)
def test_export_regexp_stays_narrow(relpath: str):
    assert media.MEDIA_RELPATH_RE.match(relpath) is None


async def test_export_file_is_served_by_signed_link(api, media_dir):
    """Локальная проверка выгрузки: подписанная ссылка из статуса job'а
    отдаёт файл (в проде тот же путь закрывает nginx secure_link)."""
    from app.services import stats as st

    name = st.export_filename(st.Period(date(2026, 7, 1), date(2026, 7, 31)), "csv", "0" * 32)
    (media_dir / st.EXPORT_DIRNAME).mkdir(parents=True, exist_ok=True)
    (media_dir / st.EXPORT_DIRNAME / name).write_bytes("Клиент;Телефон\r\n".encode("utf-8-sig"))

    url = media.signed_media_url(f"{st.EXPORT_DIRNAME}/{name}")
    r = await api.get(url)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert (await api.get(url.split("?")[0])).status_code == 403  # без подписи — 403


# -------------------------------------------------------------------- RBAC


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_upload_denied_without_messages_send(api, tokens, role):
    assert (await upload(api, tokens, PNG, role=role)).status_code == 403


async def test_upload_requires_auth(api):
    r = await api.post("/api/v1/media", files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 401


# ------------------------------------------------- прикрепление к сообщению


async def test_uploaded_media_can_be_attached_to_message(
    api, tokens, seed_conversation, db_sessionmaker
):
    """01 §6.2: attachments[{media_id}] -> метаданные в messages.attachments."""
    from sqlalchemy import select

    from app.models import Message

    media_id = (await upload(api, tokens, PNG, name="price.png")).json()["media_id"]
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json={
            "text": "Прайс во вложении",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": media_id}],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    (attachment,) = r.json()["attachments"]
    assert attachment["media_id"] == media_id
    assert attachment["kind"] == "image"
    assert attachment["name"] == "price.png"
    assert "sig=" in attachment["url"] and "exp=" in attachment["url"]

    async with db_sessionmaker() as s:
        msg = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == seed_conversation.conversation_id,
                    Message.direction == "out",
                )
            )
        ).scalar_one()
        assert msg.attachments[0]["path"].endswith(".png")


async def test_unknown_media_id_is_422(api, tokens, seed_conversation):
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json={
            "text": "с вложением",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": f"m_{uuid.uuid4().hex}"}],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "media_not_found"


async def test_more_than_five_attachments_is_422(api, tokens, seed_conversation):
    ids = [(await upload(api, tokens, PNG)).json()["media_id"] for _ in range(6)]
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json={
            "text": "много вложений",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": i} for i in ids],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "too_many_attachments"


async def test_message_without_text_but_with_attachment_is_accepted(api, tokens, seed_conversation):
    """Композер разрешает отправить одну картинку без подписи (11 §2.3)."""
    media_id = (await upload(api, tokens, JPEG, name="a.jpg", ctype="image/jpeg")).json()[
        "media_id"
    ]
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "attachments": [{"media_id": media_id}]},
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text


def test_sign_attachments_refreshes_urls_and_keeps_inbound_ones():
    """01 §6.1: подпись пересобирается на каждую сериализацию (TTL 1 ч)."""
    stored = [
        {"media_id": "m_1", "kind": "image", "name": "a.png", "size": 1, "path": "2026/08/5f/x.png"}
    ]
    (out,) = media.sign_attachments(stored)
    assert "sig=" in out["url"]
    # вложение входящего из Авито (свой формат, без path) отдаётся как есть
    inbound = [{"type": "image", "sizes": {"140x105": "https://avito/img.jpg"}}]
    assert media.sign_attachments(inbound) == inbound


# --- заметка с вложением (#30) ------------------------------------------------


async def test_a_note_keeps_the_attached_file(api, tokens, seed_conversation, db_sessionmaker):
    """Файл, приложенный к заметке, не исчезает.

    ЧТО БЫЛО. Композер прямо советует оператору: «Авито не принимает файлы от
    нас — приложите файл к заметке». Оператор прикладывал, видел вложение в
    ленте (пузырь рисуется до ответа сервера), получал 201 — и файла не было
    нигде. Схема заметки не знала поля `attachments`, лишнее поле pydantic
    отбрасывает молча, а сама заметка писала пустой список жёстко.

    То есть интерфейс уверенно направлял человека в ЕДИНСТВЕННЫЙ сценарий, где
    вложение теряется, и не подавал ни малейшего признака беды. Ровно эту
    болезнь лечила задача про молчаливую потерю вложений — но только для
    сообщений клиенту.
    """
    from sqlalchemy import select

    from app.models import Message

    media_id = (await upload(api, tokens, PNG, name="shildik.png")).json()["media_id"]
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json={
            "text": "Шильдик, модель не читается",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": media_id}],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    (attachment,) = r.json()["attachments"]
    assert attachment["media_id"] == media_id
    assert "sig=" in attachment["url"], "ссылка обязана быть подписанной, как у сообщений"

    # Ответ ручки — ещё не хранение. Проверяем саму запись: именно там
    # вложение и терялось.
    async with db_sessionmaker() as s:
        # Именно заметку, а не «первую попавшуюся строку диалога»: без фильтра
        # выборка без ORDER BY вернула бы засеянное фикстурой сообщение, и
        # тест зеленел бы на сломанном коде.
        msg = (
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == seed_conversation.conversation_id,
                        Message.direction == "note",
                    )
                )
            )
            .scalars()
            .one()
        )
        assert msg.attachments and msg.attachments[0]["media_id"] == media_id


async def test_a_note_may_be_just_a_file(api, tokens, seed_conversation):
    """Заметка «вот фото шильдика» без единого слова осмысленна.

    Раньше такая отвергалась с «Пустое сообщение»: у заметок текст проверялся
    без поблажки на вложение, в отличие от сообщений клиенту. Требовать
    подпись к картинке значит заставлять писать «фото» четыреста раз за смену.
    """
    media_id = (await upload(api, tokens, PNG, name="tabl.png")).json()["media_id"]
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json={
            "text": "",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": media_id}],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text


async def test_a_note_with_an_unknown_media_id_is_refused_loudly(api, tokens, seed_conversation):
    """Протухший идентификатор — отказ, а не тихо потерянный файл.

    Граница нужна затем же, зачем и сама починка: молчание здесь неотличимо от
    успеха, и оператор узнает о потере через неделю, когда файл понадобится.
    """
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json={
            "text": "Вот документ",
            "client_message_id": str(uuid.uuid4()),
            "attachments": [{"media_id": "m_" + "0" * 32}],
        },
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "media_not_found"
