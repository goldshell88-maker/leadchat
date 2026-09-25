"""Обращения сотрудников и служебный вход для скриптов (14 §2.1–§2.2).

Проверяем ровно то, что легко сломать незаметно:

* ответ на заявку о сбросе пароля ОДИНАКОВ для существующей и несуществующей
  учётки — включая случай «лимит исчерпан»;
* лимиты по email, по адресу и по сотруднику;
* служебная ручка не пускает по чужому токену и не пускает вообще, пока токен
  не задан;
* уведомление, которое видит администратор, написано человеческим языком, а
  подробность из bash-скрипта в заголовок не попадает никогда.

Роутеры зоны подключает ``app/main.py`` — чужой файл (см. cross-boundary).
Тесты ходят по настоящему приложению, а само подключение проверяет
``test_the_app_really_mounts_this_zone``: потерянная строка в main.py обязана
красить CI, а не тихо оставлять прод без сброса пароля.
"""

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

# ``center_svc`` — центр уведомлений, соседняя зона. Импорт ЖЁСТКИЙ: мягкий
# (try/except с пропуском) означал бы, что разъехавшийся стык — переименованный
# вид события или изменившаяся сигнатура ``notify`` — превращает сквозные
# проверки в skip. Обращение сотрудника, молча не дошедшее до администратора, —
# ровно та поломка, которую эта зона обязана исключать.
from app.api.routes import internal as internal_routes
from app.api.routes import support as support_routes
from app.services import notifications as center_svc
from app.services import support as support_svc

TOKEN = "svc-token-for-tests-0123456789"


# --- поддельный центр уведомлений --------------------------------------------


class FakeCenter:
    """Модель центра уведомлений с подавлением повторов (14 §4).

    Одинаковый ``dedup_key`` не создаёт новую запись, а увеличивает счётчик у
    существующей — именно это поведение и обещано администратору («повторялось
    12 раз» вместо двенадцати строк). ``dedup_key=None`` — политику выбирает
    каталог центра; для ``support.message`` она «не склеивать никогда».
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    async def notify(self, db: Any, redis: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        key = kwargs.get("dedup_key")
        if key is not None:
            for record in self.records:
                if record.get("dedup_key") == key:
                    record["repeat_count"] += 1
                    return
        self.records.append({**kwargs, "repeat_count": 1})

    @property
    def sent(self) -> list[dict[str, Any]]:
        return list(self.records)

    def one(self) -> dict[str, Any]:
        assert len(self.records) == 1, f"ожидали одну запись, получили {len(self.records)}"
        return self.records[0]


@pytest.fixture
def center(monkeypatch: pytest.MonkeyPatch) -> FakeCenter:
    fake = FakeCenter()
    monkeypatch.setattr(support_svc, "resolve_notify_impl", lambda: fake.notify)
    return fake


@pytest.fixture(autouse=True)
def service_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    return TOKEN


# --- клиенты ------------------------------------------------------------------
#
# Ходим по НАСТОЯЩЕМУ приложению, без досборки роутеров в тесте.
#
# Раньше здесь был помощник ``_mount``, который подмонтировал роутеры зоны, если
# не находил их среди ``app.routes``. Он был не просто лишним, а вредным: FastAPI
# ≥0.141 включает роутеры лениво (``_IncludedRouter``), в ``app.routes`` их
# путей нет вообще, поэтому «если не найдены» срабатывало ВСЕГДА — тест дублировал
# маршруты и зеленел бы даже с пустым ``app/main.py``. Ровно та тишина, которую
# эта зона обязана исключать: пропала бы и заявка на сброс пароля, и вход для
# скрипта бэкапа, а все 30 тестов остались бы зелёными.
#
# Проверку самого подключения делает ``test_the_app_really_mounts_this_zone``.


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture
async def client_from(app: FastAPI) -> AsyncIterator[Any]:
    """Клиент с заданным адресом — для проверки лимита по IP."""
    clients: list[httpx.AsyncClient] = []

    def _make(ip: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip, 12345))
        c = httpx.AsyncClient(transport=transport, base_url="https://testserver")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


def test_the_app_really_mounts_this_zone(app: FastAPI):
    """Роутеры зоны подключает ``app/main.py`` — чужой файл (см. cross-boundary).

    Обходим OpenAPI, а не ``app.routes``: лениво включённые роутеры в списке
    маршрутов не видны (тот же приём, что в ``test_rbac.py``).
    """
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/support/password-reset",
        "/api/v1/support/message",
        "/api/v1/internal/notify",
    ):
        assert path in paths, f"{path} не подключён в app/main.py"


async def _reset(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post("/api/v1/support/password-reset", json={"email": email})


# =============================================================================
# Заявка на сброс пароля: ответ ничего не выдаёт
# =============================================================================


async def test_the_answer_is_identical_for_a_real_and_a_made_up_email(
    client, users_by_role, center
):
    """Разный ответ = способ выяснить, кто у нас работает (14 §2.2)."""
    real = await _reset(client, users_by_role["manager"].email)
    fake = await _reset(client, "no-such-person@partner-lead-centre.ru")

    assert real.status_code == fake.status_code == 202
    assert real.json() == fake.json()
    # Текст — сослагательный («если такая учётная запись есть»), а не
    # утвердительный: даже формулировка не должна подтверждать существование.
    assert real.json()["message"] == support_routes.PASSWORD_RESET_ANSWER
    assert "если такая" in support_routes.PASSWORD_RESET_ANSWER.lower()


async def test_the_answer_is_identical_even_when_the_limit_is_spent(client, users_by_role, center):
    """Лимит считается ДО похода в базу — иначе 429 «только на существующих»
    сам становится ответом на вопрос «есть ли такая учётка»."""
    real_email = users_by_role["manager"].email
    for _ in range(support_svc.PASSWORD_RESET_PER_EMAIL):
        await _reset(client, real_email)
    real = await _reset(client, real_email)

    for _ in range(support_svc.PASSWORD_RESET_PER_EMAIL):
        await _reset(client, "ghost@partner-lead-centre.ru")
    fake = await _reset(client, "ghost@partner-lead-centre.ru")

    assert real.status_code == fake.status_code == 429
    assert real.json()["error"]["code"] == fake.json()["error"]["code"] == "rate_limited"
    assert real.json()["error"]["details"]["retry_after_sec"] > 0


async def test_only_a_real_account_reaches_the_admins(client, users_by_role, center):
    await _reset(client, users_by_role["manager"].email)
    await _reset(client, "nobody@partner-lead-centre.ru")

    record = center.one()
    assert record["kind"] == "support.password_reset"
    assert record["audience"] == "admin"
    assert record["entity_type"] == "user"
    assert record["entity_id"] == str(users_by_role["manager"].id)


async def test_the_admin_sees_a_human_title_and_a_one_click_button(client, users_by_role, center):
    """14 §3: заголовок — обращение к человеку, у заявки есть действие.

    Кнопку даёт каталог центра уведомлений — проверяем, что вид события,
    который порождает эта зона, в каталоге действительно с кнопкой.
    """
    await _reset(client, users_by_role["manager"].email)

    record = center.one()
    assert users_by_role["manager"].full_name in record["title"]
    assert "support." not in record["title"] and "reset" not in record["title"].lower()

    if center_svc is not None:
        action = center_svc.action_for(record["kind"])
        assert action is not None and action.label == "Выслать новую ссылку"


async def test_a_disabled_account_is_flagged_before_the_button_is_pressed(
    client, make_user, center
):
    """Кнопка «выслать ссылку» у вида события одна на всех, поэтому текст
    обязан остановить руку: одно нажатие вернуло бы доступ уволенному (01 §3.5)."""
    user = await make_user("fired@partner-lead-centre.ru", is_active=False)

    await _reset(client, user.email)

    body = center.one()["body"]
    assert "ОСТОРОЖНО" in body and "отключена" in body


async def test_repeated_requests_collapse_into_one_record(client, users_by_role, center):
    """Ключ подавления один на сотрудника: три нажатия — одна строка (14 §4)."""
    email = users_by_role["manager"].email
    for _ in range(3):
        assert (await _reset(client, email)).status_code == 202

    assert len(center.calls) == 3
    assert {call["dedup_key"] for call in center.calls} == {
        f"support.password_reset:{users_by_role['manager'].id}"
    }
    assert center.one()["repeat_count"] == 3


async def test_the_limit_per_email_holds(client, users_by_role, center):
    email = users_by_role["manager"].email
    codes = [
        (await _reset(client, email)).status_code
        for _ in range(support_svc.PASSWORD_RESET_PER_EMAIL + 1)
    ]
    assert codes[:-1] == [202] * support_svc.PASSWORD_RESET_PER_EMAIL
    assert codes[-1] == 429


async def test_the_limit_per_ip_holds_even_for_fresh_emails(client_from, center):
    """Один адрес — общий счётчик: перебором чужих адресов лимит не обойти."""
    office = client_from("203.0.113.7")
    for i in range(support_svc.PASSWORD_RESET_PER_IP):
        assert (await _reset(office, f"user{i}@partner-lead-centre.ru")).status_code == 202
    assert (await _reset(office, "one-more@partner-lead-centre.ru")).status_code == 429

    # Сосед с другого адреса при этом работает как ни в чём не бывало.
    other = client_from("198.51.100.4")
    assert (await _reset(other, "someone@partner-lead-centre.ru")).status_code == 202


async def test_the_email_counters_are_not_the_login_lockout_counters(client, users_by_role, redis):
    """Заявка «не помню пароль» не имеет права приближать блокировку входа."""
    from app.services.login_guard import lockout_retry_after

    email = users_by_role["manager"].email
    for _ in range(support_svc.PASSWORD_RESET_PER_EMAIL + 2):
        await _reset(client, email)

    assert await lockout_retry_after(redis, email, "127.0.0.1") is None


async def test_a_string_that_is_not_an_email_is_rejected(client, center):
    response = await client.post("/api/v1/support/password-reset", json={"email": "no-at-sign"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert not center.calls


# =============================================================================
# Сообщение администратору
# =============================================================================


async def _message(client: httpx.AsyncClient, token: str, **body: Any) -> httpx.Response:
    payload = {"subject": "Не печатает ценник", "text": "Не могу отправить фото клиенту"}
    payload.update(body)
    return await client.post(
        "/api/v1/support/message", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


async def test_a_message_to_the_admin_requires_a_login(client, center):
    response = await client.post(
        "/api/v1/support/message", json={"subject": "Тема", "text": "Текст"}
    )
    assert response.status_code == 401
    assert not center.calls


@pytest.mark.parametrize("role", ["admin", "head", "manager", "observer"])
async def test_every_role_can_write_to_the_admin(client, tokens, center, role):
    """Право «пожаловаться» не выдаётся — оно есть у всех, кто работает."""
    assert (await _message(client, tokens[role], subject=f"Тема {role}")).status_code == 202
    assert len(center.calls) == 1


async def test_the_message_reaches_the_admins_as_written(client, tokens, users_by_role, center):
    await _message(client, tokens["manager"], subject="Не грузится фото", text="Второй день")

    record = center.one()
    assert record["kind"] == "support.message"
    assert record["audience"] == "admin"
    assert record["severity"] == "info"
    assert users_by_role["manager"].full_name in record["title"]
    assert "Не грузится фото" in record["title"]
    assert record["body"] == "Второй день"


async def test_five_messages_per_hour_and_no_more(client, tokens, center):
    codes = [
        (await _message(client, tokens["manager"], text=f"сообщение {i}")).status_code
        for i in range(support_svc.ADMIN_MESSAGE_PER_USER + 1)
    ]
    assert codes[:-1] == [202] * support_svc.ADMIN_MESSAGE_PER_USER
    assert codes[-1] == 429


async def test_every_message_is_its_own_record(client, tokens, center):
    """Ключ склейки не задаём: в каталоге центра у «сообщения администратору»
    политика «не склеивать никогда» — два обращения не должны стать одним."""
    await _message(client, tokens["manager"], text="первое")
    await _message(client, tokens["manager"], text="второе")

    assert [call["dedup_key"] for call in center.calls] == [None, None]
    assert len(center.records) == 2
    if center_svc is not None:
        assert center_svc.KINDS["support.message"].dedup == "none"


async def test_an_empty_message_is_rejected(client, tokens, center):
    assert (await _message(client, tokens["manager"], text="")).status_code == 400
    assert (await _message(client, tokens["manager"], subject="ой")).status_code == 400
    assert not center.calls


# =============================================================================
# Служебная ручка для скриптов вне приложения
# =============================================================================


async def _internal(client: httpx.AsyncClient, token: str | None, **body: Any) -> httpx.Response:
    headers = {"X-Internal-Token": token} if token is not None else {}
    return await client.post("/api/v1/internal/notify", json=body, headers=headers)


async def test_a_foreign_token_is_refused(client, center):
    response = await _internal(client, "not-the-right-token", kind="backup.failed")
    assert response.status_code == 401
    assert not center.calls


async def test_no_token_at_all_is_refused(client, center):
    assert (await _internal(client, None, kind="backup.failed")).status_code == 401
    assert not center.calls


async def test_a_token_with_exotic_bytes_is_refused_not_five_hundred(client, center):
    """Сравнение секретов идёт по байтам: на строках с не-ASCII оно падает
    TypeError'ом, и неаутентифицированный запрос ронял бы ручку в 500."""
    response = await client.post(
        "/api/v1/internal/notify",
        json={"kind": "backup.failed"},
        headers={"X-Internal-Token": b"\xff\xfe-not-ascii"},  # сырые байты заголовка
    )
    assert response.status_code == 401
    assert not center.calls


async def test_the_endpoint_is_closed_while_the_token_is_not_configured(
    client, center, monkeypatch
):
    """Пустой токен в настройках = ручка выключена, а не «пускать всех»."""
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    assert (await _internal(client, TOKEN, kind="backup.failed")).status_code == 401
    assert (await _internal(client, "", kind="backup.failed")).status_code == 401
    assert not center.calls


async def test_a_known_event_reaches_the_admins(client, center):
    response = await _internal(
        client, TOKEN, kind="backup.failed", detail="rc=2 at line 118", source="backup.sh"
    )
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "notified": True}

    record = center.one()
    assert record["severity"] == "critical"
    assert record["title"] == "Резервное копирование не выполнилось"
    assert record["kind"] == "backup.failed"  # вид из каталога центра
    if center_svc is not None:
        # Критичное без кнопки — только с записанной причиной (14 §3).
        assert center_svc.action_for(record["kind"]) or (
            record["kind"] in center_svc.ACTIONLESS_CRITICAL
        )


async def test_the_script_cannot_write_the_title(client, center):
    """Заголовок выбирает сервер: никакая правка в bash не превратит его в stderr."""
    await _internal(client, TOKEN, kind="backup.failed", detail="rc=2 at line 118")

    record = center.one()
    assert "rc=2" not in record["title"]
    assert "rc=2 at line 118" in record["body"]  # подробность видна, но в теле


async def test_an_unknown_kind_is_refused(client, center):
    response = await _internal(client, TOKEN, kind="whatever.happened")
    assert response.status_code == 400
    assert response.json()["error"]["details"]["kind"] == "whatever.happened"
    assert "backup.failed" in response.json()["error"]["details"]["known"]
    assert not center.calls


async def test_extra_fields_are_refused(client, center):
    """Опечатка в скрипте обязана быть видна сразу, а не теряться молча."""
    response = await _internal(client, TOKEN, kind="backup.failed", severity="critical")
    assert response.status_code == 400
    assert not center.calls


async def test_a_successful_backup_leaves_a_mark_and_no_notification(client, center, redis):
    """Об успехе уведомлять нечего — отметка нужна сторожу, чтобы заметить тишину."""
    from app.scheduler.jobs.watchdog import BACKUP_OK_KEY

    response = await _internal(client, TOKEN, kind="backup.ok", source="backup.sh")
    assert response.status_code == 202
    assert response.json() == {"status": "ok", "notified": False}
    assert await redis.get(BACKUP_OK_KEY)
    assert not center.calls


async def test_repeated_failures_collapse_into_one_record(client, center):
    for _ in range(4):
        await _internal(client, TOKEN, kind="backup.failed")

    assert len(center.calls) == 4
    assert center.one()["repeat_count"] == 4


async def test_the_center_being_absent_does_not_break_the_script(client, monkeypatch):
    """Центра ещё нет — скрипт всё равно получает внятный ответ, а не 500."""
    monkeypatch.setattr(support_svc, "resolve_notify_impl", lambda: None)
    response = await _internal(client, TOKEN, kind="backup.failed")
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "notified": False}


async def test_a_broken_center_does_not_break_an_employee_request(
    client, users_by_role, monkeypatch
):
    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("центр уведомлений упал")

    monkeypatch.setattr(support_svc, "resolve_notify_impl", lambda: _explode)
    assert (await _reset(client, users_by_role["manager"].email)).status_code == 202


# =============================================================================
# Каталог видов событий
# =============================================================================


def test_every_known_kind_speaks_human():
    """Ни кода ошибки, ни вида события в заголовке (14 §3)."""
    for kind, spec in internal_routes.KNOWN_KINDS.items():
        assert spec.title and spec.title[0].isupper(), kind
        assert kind not in spec.title, kind
        assert spec.body.endswith((".", "!")), kind
        assert spec.severity in ("critical", "warning", "info"), kind


def test_every_known_kind_is_a_kind_the_center_knows():
    """Вид вне каталога центра = уведомление без иконки и без кнопки.

    Исключений больше нет: `system.unreachable` заведён в каталоге центра
    (спринт 7), и список обязан оставаться пустым — вид, о котором центр не
    знает, приходит человеку без иконки, без кнопки и без умолчаний каталога.
    """
    unknown = {
        spec.center_kind
        for spec in internal_routes.KNOWN_KINDS.values()
        if spec.center_kind not in center_svc.KINDS
    }
    assert unknown == set(), f"виды, которых нет в каталоге центра уведомлений: {sorted(unknown)}"


async def test_a_script_event_really_reaches_the_notification_center(client, db_sessionmaker):
    """Сквозная проверка стыка: без неё расхождение в сигнатуре ``notify``
    обнаруживается только на проде (уведомление молча уходит в лог)."""
    from sqlalchemy import select

    from app.models.notification import Notification

    response = await _internal(client, TOKEN, kind="backup.failed", source="backup.sh")
    assert response.json() == {"status": "accepted", "notified": True}

    async with db_sessionmaker() as session:
        rows = (await session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "backup.failed"
    assert rows[0].severity == "critical"
    assert rows[0].audience == "admin"
    assert rows[0].title == "Резервное копирование не выполнилось"


async def test_an_employee_request_really_reaches_the_notification_center(
    client, users_by_role, db_sessionmaker
):
    from sqlalchemy import select

    from app.models.notification import Notification

    await _reset(client, users_by_role["manager"].email)

    async with db_sessionmaker() as session:
        rows = (await session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "support.password_reset"
    assert rows[0].entity_id == str(users_by_role["manager"].id)
    assert center_svc.action_for(rows[0].kind) is not None  # кнопка у заявки есть


async def test_fakeredis_fixture_is_the_one_the_app_uses(client, redis):
    """Страховка: тесты лимитов бессмысленны, если приложение пишет в другой Redis."""
    assert isinstance(redis, fakeredis.aioredis.FakeRedis)
    await _reset(client, "probe@partner-lead-centre.ru")
    assert await redis.keys("support:pwreset:*")
