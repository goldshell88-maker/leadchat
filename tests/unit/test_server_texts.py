"""Тексты, которые сервер показывает человеку, и коды, по которым ветвится фронт.

Зона: `app/core/errors.py`, `app/api/routes/{auth,media,support}.py`,
`app/services/{media,support,app_settings}.py`.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ ПРОВЕРКИ ВНУТРИ ТЕСТОВ ФУНКЦИОНАЛЬНОСТИ.
Тексты ломаются не там, где их писали. Их ломает соседняя правка: кто-то
добавил статус, забыл строку в каталоге кодов — и неверный метод стал отвечать
тем же кодом, что «закрыть уже закрытый диалог». Или подключил новую ручку,
где Pydantic ответил по-английски. Такие поломки ловятся только сплошной
проверкой по всей зоне, а не тестом одной ручки.

Три вещи проверяются здесь и нигде больше:

1. **Ни одного английского слова в ответе.** Ни от фреймворка («Not Found»),
   ни от Pydantic («Field required», «Value error, ...»).
2. **Каталог кодов сплошной.** Каждый статус, который мы способны отдать,
   имеет свой код; разные беды не делят один код.
3. **Одна беда — один текст.** Проверка размера файла живёт в двух местах,
   проверка подписи — в четырёх; человек обязан прочитать одно и то же.
"""

import re
import time

import httpx
import pytest
from fastapi import FastAPI

from app.core import errors
from app.core.config import settings
from app.core.errors import ApiError
from app.services import app_settings, media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

# Латиница в тексте для человека. Исключения: слов «Авито», «LeadChat» и
# расширений файлов (jpg, png, webp, pdf) в текстах нет по-английски, а вот
# буквы в форматах — есть, поэтому проверяем не «любая латиница», а «слово
# длиннее двух букв целиком из латиницы».
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")

# Слова, которые в русском тексте для человека допустимы: это названия
# форматов, продуктов и клавиш, а не английские фразы. «email» — слово,
# которым сотрудники называют логин; в интерфейсе оно стоит и на форме входа.
_ALLOWED_LATIN = {
    "jpg",
    "jpeg",
    "png",
    "webp",
    "pdf",
    "csv",
    "xlsx",
    "leadchat",
    "email",
    "caps",
    "lock",
}


def latin_words(text: str) -> list[str]:
    """Латинские слова текста за вычетом разрешённых названий."""
    return [w for w in _LATIN_WORD.findall(text) if w.lower() not in _ALLOWED_LATIN]


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    monkeypatch.setattr(settings, "media_sign_key", "unit-test-sign-key")
    return tmp_path


@pytest.fixture
async def api(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


# =============================================================================
# 1. Английский от фреймворка не доезжает до человека
# =============================================================================


async def test_an_unknown_path_answers_in_russian(api):
    """Было: `{"code": "not_found", "message": "Not Found"}`.

    ``StarletteHTTPException`` без явного текста кладёт в ``detail``
    стандартную английскую фразу статуса, а обработчик отдавал её как есть.
    Фронт показывает `message` человеку (тосты настроек, экран входа), то есть
    диспетчер читал английское «Not Found» на русском экране.
    """
    r = await api.get("/api/v1/no-such-path")

    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "not_found"
    assert not latin_words(error["message"]), error["message"]
    assert error["message"] == errors.default_message("not_found")


async def test_a_wrong_method_answers_in_russian_and_with_its_own_code(api):
    """Было: код `unprocessable` и текст «Method Not Allowed».

    Код — половина беды, и половина хуже: `unprocessable` — это «так нельзя»
    (закрыть уже закрытый диалог), и по нему фронт показывает человеку отказ
    бизнес-правила. Неверный метод — не отказ правила, а промах клиента, и
    отличать одно от другого фронт обязан по коду.
    """
    r = await api.request("DELETE", "/api/v1/auth/login")

    assert r.status_code == 405
    error = r.json()["error"]
    assert error["code"] == "method_not_allowed"
    assert error["code"] != "unprocessable"
    assert not latin_words(error["message"]), error["message"]


def test_the_code_catalog_covers_every_status_we_can_return():
    """Пропуск в таблице статусов = чужой код в ответе.

    Именно так 405 и получил `unprocessable`: обработчик берёт код из таблицы,
    а промах таблицы заменяет «серединным» кодом. Проверяем таблицу целиком, а
    не один статус: следующий пропуск будет уже другим.
    """
    for status, code in errors._STATUS_CODES.items():
        assert 100 <= status <= 599, status
        assert errors.default_message(code) != code, f"у кода {code} нет русского текста"

    # Статусы, которые зона действительно отдаёт (415 — не тот файл,
    # 410 — истёкшая ссылка, 405 — неверный метод).
    for status in (400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429, 500, 503):
        assert status in errors._STATUS_CODES, status


def test_every_catalog_text_is_russian_and_has_no_http_jargon():
    """Каталог целиком: без латиницы и без слов из протокола.

    «Тело запроса больше лимита» человек не понимает не потому, что не знает
    слов, а потому что у него нет «тела запроса» — у него есть файл.
    """
    jargon = ("http", "multipart", "boundary", "content-type", "payload", "запрос" + "а к api")
    for code, text in errors._DEFAULT_MESSAGES.items():
        assert not latin_words(text), f"{code}: {text}"
        low = text.lower()
        for word in jargon:
            assert word not in low, f"{code}: {text}"


# =============================================================================
# 2. Pydantic отвечает по-русски
# =============================================================================


async def test_a_missing_field_is_explained_in_russian(api):
    """Было: `{"rule": "missing", "message": "Field required"}` и общий текст
    «Запрос не прошёл валидацию» сверху.

    Фронт из-за этого завёл обходной путь (`TeamMembersTab.describeError`):
    лишь бы не показать английскую строку в диспетчерской.
    """
    r = await api.post("/api/v1/auth/login", json={"email": "a@b.ru"})

    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    (field,) = error["details"]["fields"]
    assert field["field"] == "password"
    assert not latin_words(field["message"]), field["message"]
    assert not latin_words(error["message"]), error["message"]


async def test_a_too_short_password_says_how_short_in_russian(api):
    """Экран приглашения показывает `message` ответа как есть (InvitePage:147).

    Значит на первом экране нового сотрудника стояло «String should have at
    least 10 characters» либо — после общего текста — «Запрос не прошёл
    валидацию», которое не говорит вообще ничего.
    """
    r = await api.post("/api/v1/auth/invite/accept", json={"token": "t", "password": "коротко"})

    assert r.status_code == 400
    error = r.json()["error"]
    assert not latin_words(error["message"]), error["message"]
    assert "10" in error["message"], error["message"]


async def test_a_validator_message_survives_without_the_english_prefix(api):
    """Было: «Value error, нужен адрес вида имя@домен».

    Приставку клеит Pydantic перед текстом, который поднял валидатор поля;
    английские слова перед русской фразой — самый заметный след машины.

    Проверяем не только отсутствие приставки, но и СОХРАННОСТЬ текста
    валидатора: подменить его общим «значение не подходит» — тоже поломка, и
    поломка более обидная. Валидатор знает, какой именно вид нужен
    («Формат: имя@домен»), а общий текст не знает ничего.
    """
    r = await api.post("/api/v1/support/password-reset", json={"email": "без-собаки"})

    assert r.status_code == 400
    error = r.json()["error"]
    assert "Value error" not in error["message"]
    assert not latin_words(error["message"]), error["message"]
    assert error["message"] == "Похоже, это не адрес почты. Формат: имя@домен"
    (field,) = error["details"]["fields"]
    assert field["message"] == error["message"]


def test_an_unknown_pydantic_rule_still_answers_in_russian():
    """Правило, которого нет в словаре, обязано дать русский текст.

    Иначе словарь чинит только сегодняшние правила: первая же новая проверка
    в схеме вернула бы английский msg, и мы бы об этом не узнали.
    """
    assert errors.rule_message("совершенно_новое_правило") == "Значение не подходит"
    assert not latin_words(errors.rule_message("совершенно_новое_правило"))


def test_a_rule_with_broken_context_does_not_leak_a_template():
    """У правила с подстановкой может не оказаться нужного ctx — тогда текст
    обязан остаться человеческим, а не показать «{min_length}»."""
    text = errors.rule_message("string_too_short", {"чужой_ключ": 1})
    assert "{" not in text and "}" not in text, text


# =============================================================================
# 3. Заголовок Retry-After на 429 (01 §1.3)
# =============================================================================


async def test_a_rate_limited_answer_carries_retry_after(api, make_user):
    """01 §1.3: «ответ несёт заголовок Retry-After (сек)».

    Срок лежал только в `details.retry_after_sec`, а details читает лишь наш
    фронт. Заголовок понимают все — curl, десктопная обёртка, любой прокси.
    """
    await make_user("limited@partner-lead-centre.ru")
    last = None
    for _ in range(support_reset_attempts()):
        last = await api.post(
            "/api/v1/support/password-reset", json={"email": "limited@partner-lead-centre.ru"}
        )

    assert last is not None and last.status_code == 429
    assert last.json()["error"]["code"] == "rate_limited"
    assert int(last.headers["Retry-After"]) > 0


def support_reset_attempts() -> int:
    from app.services import support

    return support.PASSWORD_RESET_PER_EMAIL + 1


def test_retry_after_is_not_glued_to_answers_that_are_not_429():
    """Заголовок ставится по статусу, а не по наличию поля в details.

    Блокировка входа отвечает 403 и тоже несёт `retry_after_sec`; Retry-After
    на 403 стандартом не определён, и прокси на нём ведут себя по-разному.
    """
    response = errors._envelope(403, "account_locked", "текст", {"retry_after_sec": 60})
    assert "retry-after" not in {k.lower() for k in response.headers}


# =============================================================================
# 4. Вложения: одна беда — один текст
# =============================================================================


async def test_a_broken_upload_never_mentions_http_headers(api, tokens, media_dir):
    """Было: «Ожидается multipart/form-data с полем 'file'» и «В Content-Type
    не найден boundary».

    Диспетчер не знает, что такое boundary, и починить его не может. Ему нужно
    одно действие — приложить файл заново; оно и написано.
    """
    r = await api.post(
        "/api/v1/media",
        content=b"{}",
        headers={**auth(tokens), "Content-Type": "application/json"},
    )

    assert r.status_code == 400
    error = r.json()["error"]
    assert error["message"] == media.UPLOAD_BROKEN_MESSAGE
    assert not latin_words(error["message"]), error["message"]
    assert error["details"]["reason"] == "not_multipart"


def test_all_three_broken_upload_paths_say_the_same_thing():
    """Три причины — один текст и один код, причина живёт в details.

    Пока текст писался по месту, каждая ветка объясняла беду своими словами:
    человек по ним не мог понять, разные это поломки или одна.
    """
    # Третий случай — настоящее multipart-тело с файлом, но в поле с чужим
    # именем: boundary есть, части есть, части ``file`` нет.
    wrong_field = httpx.Request("POST", "https://x/", files={"вложение": ("a.png", PNG)})

    reasons = []
    for body, ctype in (
        (b"{}", "application/json"),  # не multipart
        (b"--x\r\n", "multipart/form-data"),  # нет boundary
        (wrong_field.read(), wrong_field.headers["content-type"]),
    ):
        with pytest.raises(ApiError) as exc:
            media.parse_multipart_file(body, ctype)
        assert exc.value.message == media.UPLOAD_BROKEN_MESSAGE
        assert exc.value.code == "validation_error"
        reasons.append(exc.value.details["reason"])

    # Причины при этом РАЗНЫЕ: одинаковый текст человеку не значит одинаковый
    # разбор в логе.
    assert len(set(reasons)) == 3, reasons


def test_the_size_limit_speaks_with_one_voice(monkeypatch):
    """Одна беда, два места обнаружения, один текст.

    Роутер обрывает закачку по Content-Length, не дожидаясь конца, а сервис
    домеряет уже разобранную часть. Пока текст был написан дважды, разъезд в
    лимите или в формулировке заметить было нечем.
    """
    monkeypatch.setattr(settings, "media_max_size_mb", 3)
    from_stream = media.too_large_error()
    from_parse = media.too_large_error(size=99)

    assert from_stream.message == from_parse.message
    assert from_stream.code == from_parse.code == "payload_too_large"
    assert "3 МБ" in from_stream.message
    assert from_parse.details["size"] == 99


async def test_both_size_checks_answer_with_that_same_text(api, tokens, media_dir, monkeypatch):
    """Обе двери — и обрыв закачки, и разбор — говорят одно и то же.

    Проверять надо именно ОБЕ. Файл чуть больше лимита роутер пропускает
    (у него запас на multipart-обвязку) и отвергает уже разбор; файл больше
    лимита с запасом до разбора не доходит вовсе. Пока тест бил только во
    вторую дверь, текст в первой можно было испортить незаметно — так и
    вышло при проверке этого теста на живой поломке.
    """
    monkeypatch.setattr(settings, "media_max_size_mb", 1)
    expected = media.too_large_error().message

    # Дверь вторая: разбор. Тело влезает в потолок роутера (лимит + 8 КБ).
    at_parse = await api.post(
        "/api/v1/media",
        files={"file": ("big.png", PNG[:8] + b"\x00" * (1024 * 1024 + 1), "image/png")},
        headers=auth(tokens),
    )
    assert at_parse.status_code == 413
    assert at_parse.json()["error"]["message"] == expected

    # Дверь первая: роутер обрывает чтение, не дожидаясь конца закачки.
    at_stream = await api.post(
        "/api/v1/media",
        files={"file": ("huge.png", PNG[:8] + b"\x00" * (1024 * 1024 + 64 * 1024), "image/png")},
        headers=auth(tokens),
    )
    assert at_stream.status_code == 413
    assert at_stream.json()["error"]["message"] == expected


async def test_a_forbidden_file_type_tells_what_fits(api, tokens, media_dir):
    """415 обязан назвать, ЧТО подойдёт, — иначе человек будет пробовать
    наугад: следующим он приложит .doc, потом .zip."""
    r = await api.post(
        "/api/v1/media",
        files={"file": ("payload.png", b"\x7fELF" + b"\x00" * 64, "image/png")},
        headers=auth(tokens),
    )

    assert r.status_code == 415
    message = r.json()["error"]["message"]
    assert "pdf" in message and "png" in message
    assert not latin_words(message), message


# =============================================================================
# 5. Ссылка на файл: два кода на две разные беды, а не «forbidden» на всё
# =============================================================================


def test_a_link_without_a_signature_and_a_tampered_one_read_the_same(media_dir):
    """Было три текста на одну беду: «Ссылка без подписи» и «Ссылка с
    некорректной подписью» дважды. Человек, открывший вчерашнюю вкладку, читал
    любой из трёх и не понимал, чем они отличаются, — а не отличаются ничем.
    """
    relpath = "2026/08/5f/5f6a3c2e-0000-4000-8000-000000000001.jpg"
    messages = set()
    for sig, exp in ((None, None), ("подделка", int(time.time()) + 60), ("s", "не-число")):
        with pytest.raises(ApiError) as exc:
            media.check_signature(relpath, sig, exp)
        assert exc.value.status == 403
        assert exc.value.code == "media_link_invalid"
        messages.add(exc.value.message)

    assert messages == {media.LINK_INVALID_MESSAGE}


def test_a_broken_link_is_not_a_rights_problem(media_dir):
    """Код НЕ ``forbidden``.

    ``forbidden`` в этой системе означает «у вас нет прав»: по нему экран входа
    рисует «Учётная запись отключена», а интерфейс — отказ роли. К сломанной
    ссылке на файл права сотрудника отношения не имеют.
    """
    with pytest.raises(ApiError) as exc:
        media.check_signature("2026/08/5f/x.jpg", None, None)

    assert exc.value.code != "forbidden"


def test_an_expired_link_is_its_own_code(media_dir):
    """Истёкшая ссылка чинится сама: фронт перезапросил диалог — получил
    свежую подпись. Битая не чинится ничем. Разные беды — разные коды."""
    relpath = "2026/08/5f/5f6a3c2e-0000-4000-8000-000000000001.jpg"
    url = media.signed_media_url(relpath, ttl=-10)
    sig = re.search(r"sig=([^&]+)", url).group(1)
    exp = re.search(r"exp=([^&]+)", url).group(1)

    with pytest.raises(ApiError) as exc:
        media.check_signature(relpath, sig, exp)

    assert exc.value.status == 410
    assert exc.value.code == "media_link_expired"
    assert exc.value.code != "media_link_invalid"
    assert exc.value.message == media.LINK_EXPIRED_MESSAGE


# =============================================================================
# 6. Вход и профиль
# =============================================================================


async def test_a_wrong_current_password_is_not_the_login_code(api, tokens, users_by_role):
    """Смена своего пароля отвечала кодом `invalid_credentials` — тем самым,
    которым отвечает неудачный ВХОД.

    По этому коду экран входа чистит поле пароля, трясёт форму и уводит фокус.
    Здесь человек уже внутри и всего лишь опечатался в текущем пароле: беда
    другая, и вести себя с ней надо иначе — значит и код обязан быть другим.
    """
    r = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": "не тот", "new_password": "достаточно длинный"},
        headers=auth(tokens),
    )

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "wrong_current_password"
    assert error["code"] != "invalid_credentials"
    assert error["details"]["reason"] == "wrong_current_password"


async def test_a_disabled_account_hears_about_the_account_not_about_rights(api, make_user):
    """Отключённой учётке говорят, что случилось с УЧЁТКОЙ, а не про права.

    Общий текст кода `forbidden` — «Здесь нужны права выше ваших»; человеку,
    которого отключили, он врёт: права у него те же, что вчера. Поэтому вход
    подставляет свой текст.
    """
    user = await make_user("fired@partner-lead-centre.ru", is_active=False)

    login = await api.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "correct horse battery staple"},
    )

    assert login.status_code == 403
    assert login.json()["error"]["message"] == errors.ACCOUNT_DISABLED_MESSAGE
    assert "прав" not in login.json()["error"]["message"]


async def test_the_support_form_says_the_same_about_a_disabled_account(make_user):
    """Тот же текст во второй двери — форме «Написать администратору».

    Ручка проверяется НАПРЯМУЮ, а не по HTTP, и это не упрощение: по HTTP до
    неё не доходит очередь. Общая зависимость `get_current_user`
    (`app/api/deps.py` — вне этой зоны) отсекает выключенного сотрудника
    раньше и отвечает голым `forbidden` с текстом про права. Страховка внутри
    ручки от этого не лишняя (зависимости меняют), но текст её виден только
    так. Про `deps.py` — в отчёт: правка чужого файла.
    """
    from app.api.routes.support import AdminMessageIn, admin_message

    user = await make_user("fired2@partner-lead-centre.ru", is_active=False)
    body = AdminMessageIn(subject="Тема письма", text="Текст")

    with pytest.raises(ApiError) as exc:
        await admin_message(body=body, user=user, db=None, redis=None)

    assert exc.value.status == 403
    assert exc.value.message == errors.ACCOUNT_DISABLED_MESSAGE


# =============================================================================
# 7. Словарь терминов (10 §7.2) в текстах зоны
# =============================================================================

#: «Говорим / Не говорим» — левый столбец таблицы 10 §7.2 нас не касается,
#: важен правый: этих слов в текстах для человека быть не должно.
BANNED_WORDS = ("обращени", "оператор", "юзер", "анрид", "листинг", "тикет")


def zone_texts() -> dict[str, str]:
    """Все тексты зоны, которые видит человек, в одном месте."""
    texts = {f"errors:{code}": text for code, text in errors._DEFAULT_MESSAGES.items()}
    texts.update({f"rule:{rule}": text for rule, text in errors._RULE_MESSAGES.items()})
    texts["errors:account_disabled"] = errors.ACCOUNT_DISABLED_MESSAGE
    texts["media:upload_broken"] = media.UPLOAD_BROKEN_MESSAGE
    texts["media:link_invalid"] = media.LINK_INVALID_MESSAGE
    texts["media:link_expired"] = media.LINK_EXPIRED_MESSAGE
    texts["media:file_missing"] = media.FILE_MISSING_MESSAGE
    texts["media:too_large"] = media.too_large_error().message
    return texts


def test_no_text_of_this_zone_breaks_the_glossary():
    """Словарь 10 §7.2 утверждён и был нарушен в 28 местах интерфейса.

    Проверяем свою зону сплошняком, а не глазами: следующий текст напишут
    через месяц, и «оператор» вернётся ровно так же, как пришёл в прошлый раз.
    """
    for name, text in zone_texts().items():
        low = text.lower()
        for word in BANNED_WORDS:
            assert word not in low, f"{name}: «{word}» в тексте «{text}»"


def test_the_settings_texts_talk_about_dialogs_not_about_tickets():
    """Тексты настроек уезжают на экран как есть (`e.message` в
    DistributionTab и WorkHoursBlock), поэтому словарь их касается напрямую."""
    with pytest.raises(ApiError) as exc:
        app_settings._validate(app_settings.SPECS[app_settings.DISTRIBUTION_MAX_ACTIVE], 10_000)

    assert "диалог" in exc.value.message.lower()
    assert not latin_words(exc.value.message), exc.value.message


def test_every_settings_refusal_is_human_readable():
    """Ни одного «Ожидалось <тип>»: человек не выбирал тип, он выбирал число.

    Тип нужен разработчику и остаётся в `details.fields[].rule`.
    """
    bad_values = [
        (app_settings.DISTRIBUTION_ENABLED, "да"),
        (app_settings.DISTRIBUTION_MAX_ACTIVE, "пять"),
        (app_settings.DISTRIBUTION_MAX_ACTIVE, 0),
        (app_settings.STATS_WORK_START_HOUR, "десять"),
        (app_settings.STATS_WORK_START_HOUR, 42),
    ]
    for key, value in bad_values:
        with pytest.raises(ApiError) as exc:
            app_settings._validate(app_settings.SPECS[key], value)
        message = exc.value.message
        assert not latin_words(message), f"{key}={value!r}: {message}"
        assert "ожида" not in message.lower(), f"{key}={value!r}: {message}"
