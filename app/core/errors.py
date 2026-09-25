"""Конверт ошибки (01 §1.3).

Любой не-2xx ответ — один и тот же JSON:

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "req_..."}}

Один обработчик поверх ``ApiError`` (его бросает прикладной код) плюс
переводчики для ``HTTPException`` фреймворка и ошибок Pydantic.

ЧТО ЗДЕСЬ ВАЖНО ПОНИМАТЬ ПРО ``message``. Это не строчка для лога: фронт
показывает её человеку как есть (``err.message`` в тостах настроек, на экране
входа, в окне приглашения). Поэтому три требования к каждому тексту каталога:
по-русски, без канцелярита и с ответом на вопрос «что мне теперь делать»
(10 §7.1). Код ошибки — машинный и стабильный, текст — человеческий.

ДВЕ БЕДЫ, КОТОРЫЕ ЛЕЧИТ ЭТОТ МОДУЛЬ ЦЕЛИКОМ, А НЕ ПО МЕСТУ ВЫЗОВА:

1. **Английский от фреймворка.** ``StarletteHTTPException`` без явного текста
   подставляет в ``detail`` стандартную английскую фразу статуса, и она
   доезжала до человека дословно: на несуществующий путь приходило
   ``{"code": "not_found", "message": "Not Found"}``, на неверный метод —
   ``"Method Not Allowed"``. То же с Pydantic: ``"Field required"``,
   ``"String should have at least 10 characters"`` и приставка
   ``"Value error, "`` перед русским текстом валидатора. Фронт даже завёл
   обходной путь (``TeamMembersTab.describeError``), лишь бы не показывать
   английскую строку в диспетчерской. Переводим здесь — в одном месте.

2. **Разъехавшиеся коды.** Код выбирался по таблице статусов, а в таблице не
   было 405/410/415. Неверный метод получал ``unprocessable`` — тот же код,
   которым отвечает «закрыть уже закрытый диалог». Фронт по такому коду не
   отличит «повторите» от «так нельзя». Таблица ниже покрывает каждый статус,
   который мы способны отдать.
"""

import http
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

log = structlog.get_logger("app.errors")

# Каталог общих кодов (01 §1.3); именные коды несут свой текст.
#
# Таблица обязана покрывать ВСЕ статусы, которые способен вернуть сервис, —
# включая те, что рождаются внутри фреймворка (404 на неизвестный путь, 405 на
# неверный метод). Пропуск в таблице означает не «нет кода», а «чужой код»:
# 405 сваливался в ``unprocessable`` и становился неотличим от отказа
# бизнес-правила.
_STATUS_CODES: dict[int, str] = {
    400: "validation_error",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "rate_limited",
    500: "internal_error",
    503: "upstream_unavailable",
}

_DEFAULT_MESSAGES: dict[str, str] = {
    "validation_error": "Проверьте заполнение полей и повторите",
    "unauthorized": "Сессия закончилась — войдите заново",
    "forbidden": "Здесь нужны права выше ваших — попросите администратора",
    "not_found": "Не нашли — возможно, это уже удалили. Обновите страницу",
    # 405 рождается, когда клиент собрал запрос не так, — вины человека тут нет
    # и объяснять ему нечего. Даём нейтральное «не получилось» и действие.
    "method_not_allowed": "Не получилось выполнить действие — обновите страницу и повторите",
    "conflict": "Это уже изменили без вас — обновите страницу и посмотрите, как стало",
    "gone": "Это больше не действует — обновите страницу",
    "payload_too_large": "Слишком много данных за раз — уменьшите и повторите",
    "unsupported_media_type": "Такой файл отправить нельзя",
    "unprocessable": "Сейчас так нельзя — обновите страницу и посмотрите, что изменилось",
    "rate_limited": "Слишком часто. Подождите немного и повторите",
    "internal_error": "Сбой на нашей стороне. Повторите через минуту — если не пройдёт, "
    "сообщите администратору",
    # Не «Авито не отвечает»: этим же кодом отвечают очередь выгрузок и AI.
    # Врать про виновника нельзя, поэтому оба вызывающих передают свой текст,
    # а здесь — честный общий.
    "upstream_unavailable": "Внешняя система не отвечает. Повторите через минуту",
    # именные коды (01 §1.3, список под таблицей)
    "invalid_credentials": "Неверный email или пароль",
    "account_locked": "Вход временно закрыт: слишком много неверных попыток. "
    "Подождите или попросите администратора выслать новую ссылку",
    "invite_expired": "Ссылка не работает: устарела или её уже использовали. "
    "Попросите у администратора новую",
    # Текст дословно из 10 §7.3 — его же показывает плашка «Режим просмотра».
    "read_only_role": "Режим просмотра — отвечать может менеджер. "
    "Назначьте менеджера или передайте диалог",
}

# Правила Pydantic по-русски. Ключ — ``type`` из ``exc.errors()``.
#
# Зачем словарь, а не «показывать msg как есть»: ``msg`` у Pydantic английский
# («Field required»), и никакой настройкой это не переключается. Незнакомое
# правило падает в ``_FALLBACK_RULE`` — так английский не просочится даже из
# правила, которого здесь ещё нет.
_FALLBACK_RULE = "Значение не подходит"

_RULE_MESSAGES: dict[str, str] = {
    "missing": "Заполните обязательное поле",
    "string_too_short": "Слишком коротко: нужно не меньше {min_length} символов",
    "string_too_long": "Слишком длинно: не больше {max_length} символов",
    "too_short": "Слишком мало значений: нужно не меньше {min_length}",
    "too_long": "Слишком много значений: не больше {max_length}",
    "string_type": "Нужен текст",
    "string_pattern_mismatch": "Значение не подходит по формату",
    "int_type": "Нужно целое число",
    "int_parsing": "Нужно целое число",
    "float_type": "Нужно число",
    "float_parsing": "Нужно число",
    "bool_type": "Нужно «да» или «нет»",
    "bool_parsing": "Нужно «да» или «нет»",
    "greater_than": "Нужно больше {gt}",
    "greater_than_equal": "Нужно не меньше {ge}",
    "less_than": "Нужно меньше {lt}",
    "less_than_equal": "Нужно не больше {le}",
    "enum": "Такого значения нет в списке",
    "literal_error": "Такого значения нет в списке",
    "uuid_parsing": "Это не идентификатор",
    "uuid_type": "Это не идентификатор",
    "date_parsing": "Неверная дата",
    "date_type": "Неверная дата",
    "date_from_datetime_parsing": "Неверная дата",
    "datetime_parsing": "Неверные дата и время",
    "datetime_type": "Неверные дата и время",
    "extra_forbidden": "Такого поля здесь нет",
    "json_invalid": "Запрос не разобрать — обновите страницу и повторите",
    "list_type": "Нужен список значений",
    "dict_type": "Нужен набор полей",
    "model_attributes_type": "Нужен набор полей",
    "value_error": _FALLBACK_RULE,  # перекрывается текстом самого валидатора
}

# Pydantic приклеивает это перед текстом, который поднял валидатор поля:
# ``ValueError("нужен адрес вида имя@домен")`` приезжает как
# ``"Value error, нужен адрес вида имя@домен"``. Английская приставка перед
# русской фразой — самый заметный след машины в тексте для человека.
_PYDANTIC_VALUE_ERROR_PREFIX = "Value error, "

# Заголовок для 429 (01 §1.3: «ответ несёт заголовок Retry-After»). Срок мы
# уже кладём в ``details.retry_after_sec``, но details читает только наш фронт;
# заголовок понимают все — от curl до десктопной обёртки.
RETRY_AFTER_DETAIL_KEY = "retry_after_sec"

#: Отключённой учётке отвечают несколько ручек, и человек обязан прочитать
#: одно и то же — иначе «отключили» выглядит как разные поломки в разных
#: местах. Экран входа берёт этот текст из ответа как есть.
ACCOUNT_DISABLED_MESSAGE = "Учётная запись отключена, обратитесь к администратору"


class ApiError(Exception):
    """Прикладная ошибка со стабильным машинным кодом."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_MESSAGES.get(code, code)
        self.status = status
        self.details = details
        super().__init__(f"{status} {code}: {self.message}")


def default_message(code: str) -> str:
    """Текст каталога по коду (для тестов и для вызывающих, которым он нужен)."""
    return _DEFAULT_MESSAGES.get(code, code)


def rule_message(rule: str, ctx: dict[str, Any] | None = None) -> str:
    """Правило Pydantic → русская фраза. Незнакомое правило — общий текст."""
    template = _RULE_MESSAGES.get(rule, _FALLBACK_RULE)
    if ctx:
        try:
            return template.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return _FALLBACK_RULE
    return template


def _retry_after_header(status: int, details: dict[str, Any] | None) -> dict[str, str] | None:
    if status != 429 or not details:
        return None
    value = details.get(RETRY_AFTER_DETAIL_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return {"Retry-After": str(value)}


def _envelope(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    error["request_id"] = request_id_var.get()
    return JSONResponse(
        status_code=status,
        content={"error": error},
        headers=_retry_after_header(status, details),
    )


async def _api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return _envelope(exc.status, exc.code, exc.message, exc.details)


def _is_stock_http_phrase(status: int, detail: str) -> bool:
    """``detail`` — это стандартная английская фраза статуса, а не наш текст?

    ``StarletteHTTPException`` без явного ``detail`` подставляет
    ``HTTPStatus(status).phrase`` — «Not Found», «Method Not Allowed». Отличить
    её от осмысленного текста можно только сравнением: поле одно и то же.
    """
    try:
        return detail == http.HTTPStatus(status).phrase
    except ValueError:  # нестандартный статус — стандартной фразы у него нет
        return False


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(
        exc.status_code, "internal_error" if exc.status_code >= 500 else "unprocessable"
    )
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail.strip() else None
    if detail is not None and _is_stock_http_phrase(exc.status_code, detail):
        detail = None
    return _envelope(exc.status_code, code, detail or default_message(code))


def _field_error(err: dict[str, Any]) -> dict[str, str]:
    """Одна строка ``details.fields[]``: имя поля, правило и русский текст."""
    rule = str(err.get("type", ""))
    message = str(err.get("msg", ""))
    if rule == "value_error" and message.startswith(_PYDANTIC_VALUE_ERROR_PREFIX):
        # Текст поднял валидатор поля — он и есть самый точный: там написано
        # «нужен адрес вида имя@домен», а не «значение не подходит».
        message = message[len(_PYDANTIC_VALUE_ERROR_PREFIX) :].strip()
    else:
        message = rule_message(rule, err.get("ctx"))
    return {
        "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body"),
        "rule": rule,
        "message": message or _FALLBACK_RULE,
    }


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = [_field_error(err) for err in exc.errors()]
    # Одно поле — говорим про него: «Слишком коротко: нужно не меньше 10
    # символов» человек починит, а «Проверьте заполнение полей» на экране
    # приглашения (единственное поле — пароль) не говорит ничего. Полей
    # несколько — общий текст: перечислять их в одну строку нечитаемо, разбор
    # по полям фронт берёт из ``details.fields``.
    message = fields[0]["message"] if len(fields) == 1 else default_message("validation_error")
    return _envelope(400, "validation_error", message, {"fields": fields})


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path)
    return _envelope(500, "internal_error", default_message("internal_error"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
