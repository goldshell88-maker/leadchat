"""Отказ nginx по частоте отвечает конвертом API с `Retry-After` (проверка 24.09).

ЧТО СЛУЧИЛОСЬ. Лимит `api` отвечал HTML-страницей nginx. Фронт не находил в ней
конверта, показывал «Запрос завершился с кодом 429» и ничего не повторял: 5 583
таких отказа за 08.09–24.09, на диалоге, где человек остановился после листания,
— «Диалог недоступен», сорванные «Принять».

ЧТО СТЕРЕЖЁМ (сторож формы, как и `test_nginx_ratelimit_0609`):

1. `location /api/` и вход отдают отказ лимита в `@api_too_many`;
2. там — JSON-конверт с кодом `too_many_requests`, `Retry-After` и no-store;
3. код совпадает с тем, по которому фронт решает «подождать и повторить»
   (`TOO_MANY_REQUESTS` в frontend/src/shared/api/http.ts), — разойдись они, и
   фронт снова читал бы отказ как окончательный;
4. в `/api/` не включён `proxy_intercept_errors`: иначе обработчик подменял бы
   и собственные 429 приложения (`rate_limited` дневного лимита выгрузок), а
   фронт принялся бы их повторять.

Поведение проверено руками 24.09 на `nginx:1.27-alpine` с настоящими
nginx.conf, сниппетами и шаблоном: `nginx -t` зелёный на старом и новом
шаблоне; 150 одновременных запросов одним токеном к подставному api, который
сам отвечает 429 `rate_limited`, — 76 × 429 `rate_limited` (ответ приложения
прошёл нетронутым) и 74 × 429 `too_many_requests` с `Retry-After: 1`,
`Cache-Control: no-store` и заголовками защиты.
"""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
VHOST = ROOT / "docker/nginx/templates/leadchat.conf.template"
FRONT_HTTP = ROOT / "frontend/src/shared/api/http.ts"


def _lines() -> list[str]:
    out = []
    for raw in VHOST.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def _block(lines: list[str], header: str) -> list[str]:
    pattern = re.compile(header)
    for i, line in enumerate(lines):
        if pattern.search(line):
            depth = line.count("{") - line.count("}")
            body: list[str] = []
            for inner in lines[i + 1 :]:
                depth += inner.count("{") - inner.count("}")
                if depth <= 0:
                    return body
                body.append(inner)
    raise AssertionError(f"в шаблоне нет блока {header!r}")


def test_api_and_login_send_rate_limit_rejections_to_json_handler() -> None:
    lines = _lines()
    for header in (r"^location /api/ \{", r"^location = /api/v1/auth/login \{"):
        assert "error_page 429 @api_too_many;" in _block(lines, header), header


def test_handler_answers_with_api_envelope_and_retry_after() -> None:
    body = _block(_lines(), r"^location @api_too_many \{")
    assert "default_type application/json;" in body
    assert 'add_header Retry-After "1" always;' in body
    assert 'add_header Cache-Control "no-store" always;' in body
    assert "include /etc/nginx/snippets/security-headers.conf;" in body

    answer = next(line for line in body if line.startswith("return "))
    status, payload = re.fullmatch(r"return (\d+) '(.+)';", answer).groups()  # type: ignore[union-attr]
    assert status == "429"
    envelope = json.loads(payload)
    assert envelope["error"]["code"] == "too_many_requests"
    assert envelope["error"]["message"]


def test_frontend_waits_on_the_same_code() -> None:
    front = FRONT_HTTP.read_text(encoding="utf-8")
    code = re.search(r'export const TOO_MANY_REQUESTS = "([a-z_]+)";', front)
    assert code, "фронт больше не объявляет код отказа nginx"
    assert code.group(1) == "too_many_requests"


def test_api_location_does_not_intercept_application_errors() -> None:
    body = _block(_lines(), r"^location /api/ \{")
    assert not any(line.startswith("proxy_intercept_errors") for line in body)
