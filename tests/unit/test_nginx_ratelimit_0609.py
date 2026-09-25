"""Зона rate-limit `api` в nginx считает на ЧЕЛОВЕКА, а не на адрес (06.09).

ЧТО СЛУЧИЛОСЬ. Замер журнала nginx за 24 ч 06.09: 344–364 ответа 429, все с
одного remote_addr — офисный NAT, 94,8 % трафика — и все с пустым upstream_rt,
то есть отбил сам nginx. Пачками: 07:00 UTC — 144, 17:00 — 125, 03:00 — 26;
по ручкам: counts 113, list 68, read 40, messages 33, identity 23,
merge-candidates 21, claim 17. Зона `api` шла по `$binary_remote_addr` с
планкой 30r/s: 13 человек по ≈2,6 r/s делили её на всех, а react-query 429 не
повторяет — человек видел «Не получилось загрузить» и сорванное «Принять».

ЧТО СТЕРЕЖЁМ. Три вещи в тексте конфига:

1. зона `api` ключом `$lc_rl_key`, а `$lc_rl_key` — это map по хвосту
   `Authorization: Bearer …` с умолчанием `$binary_remote_addr` (без токена —
   адрес, как раньше). Хвост обязан лежать ВНУТРИ подписи JWT: заголовок и
   начало payload у всех токенов одинаковые, уникальна только подпись, и
   ключ из общей части свёл бы всю контору в один счётчик, как и было;
2. страховочная зона `api_addr` по адресу с потолком выше личного: nginx
   подпись не проверяет, и без неё сканер с мусорным Bearer получал бы
   свежий ключ на каждый запрос и проходил мимо лимита целиком;
3. ключ — кусок подписи, то есть секрет, и в access_log он не попадает.

⚠ ЧЕСТНАЯ ОГОВОРКА: это сторож ФОРМЫ, он читает текст конфига. Поведение
nginx из pytest не поднять: конфигу нужны образ с envsubst-entrypoint'ом,
сертификат по имени домена и резолв upstream'а `api`. Поведение проверено
руками 07.09 на `nginx:1.27-alpine` из compose с настоящим шаблоном
(`docker run … nginx -t` зелёный; мёртвый upstream даёт 502 пропущенным и
429 отбитым): 100 запросов одним токеном — 62 прошли, 38 × 429; 100 запросов
100 разными токенами — 0 × 429; 100 без токена — 38 × 429 по адресу; 900
мусорных токенов за 0,32 с — 436 × 429 от зоны `api_addr`; в error.log —
только `by zone "api"`/`"api_addr"` и адрес, значение ключа не пишется.
Единственное, что здесь проверяется по-настоящему, — что regex из map,
приложенный к токену от `create_access_token`, вырезает ровно подпись.

⚠ ДИВЕРСИЯ: вернуть в `nginx.conf` строку
`limit_req_zone $binary_remote_addr zone=api:10m rate=30r/s;` — краснеет
`test_зона_api_считает_по_ключу_человека`. Убрать `limit_req zone=api_addr`
из `location /api/` — краснеет `test_страховочная_зона_по_адресу_стоит_поверх`.
Вписать `$lc_rl_key` в `log_format` — краснеет `test_ключ_не_попадает_в_журнал`.
Поменять `{43}` в map на `{60}` — краснеет
`test_хвост_из_map_вырезает_ровно_подпись_настоящего_токена`.
Ревью 07.09, ещё 17 диверсий, все красные: убрать `.+` перед группой (живой
токен перестаёт совпадать, ключ тихо = адрес) и `{43}`→`{20}` — тот же
сторож по живому токену; `~`→`~*`, снять `^`, сменить имя переменной-значения
или источник map — сторожа карты; `api_addr` по `$lc_rl_key`, rate 20r/s,
burst api 500, снять любую из двух `limit_req` в /api/ — сторож страховки;
`zone=api` в login — сторож единственного location; `$http_authorization` в
log_format, `add_header … $lc_rl_key` в vhost, `proxy_set_header … $lc_rl_key`
в nginx.conf — сторож журнала.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.core.security import create_access_token

ROOT = pathlib.Path(__file__).resolve().parents[2]
NGINX_CONF = ROOT / "docker/nginx/nginx.conf"
VHOST = ROOT / "docker/nginx/templates/leadchat.conf.template"

КЛЮЧ = "$lc_rl_key"


def _без_комментариев(path: pathlib.Path) -> list[str]:
    """Строки конфига без пояснений: `#` в начале — текст, а не директива."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def _блок(lines: list[str], начало: re.Pattern[str]) -> list[str]:
    """Тело первого блока `{ … }`, чей заголовок совпал с `начало`."""
    for i, line in enumerate(lines):
        if начало.search(line):
            глубина = line.count("{") - line.count("}")
            тело: list[str] = []
            for inner in lines[i + 1 :]:
                глубина += inner.count("{") - inner.count("}")
                if глубина <= 0:
                    return тело
                тело.append(inner)
    return []


def _группа(pattern: str, text: str, name: str | int = 1) -> str:
    m = re.search(pattern, text)
    assert m, f"{pattern!r} не совпал с {text!r}"
    return m.group(name)


def _зона(lines: list[str], имя: str) -> re.Match[str] | None:
    шаблон = re.compile(rf"^limit_req_zone\s+(\S+)\s+zone={имя}:(\d+)m\s+rate=(\d+)r/s;")
    for line in lines:
        m = шаблон.match(line)
        if m:
            return m
    return None


@pytest.fixture(scope="module")
def nginx_conf() -> list[str]:
    assert NGINX_CONF.is_file(), "nginx.conf исчез — сторож проверяет несуществующий файл"
    return _без_комментариев(NGINX_CONF)


@pytest.fixture(scope="module")
def vhost() -> list[str]:
    assert VHOST.is_file(), "шаблон vhost исчез — сторож проверяет несуществующий файл"
    return _без_комментариев(VHOST)


@pytest.fixture(scope="module")
def карта(nginx_conf: list[str]) -> list[str]:
    """Тело `map $http_authorization $lc_rl_key { … }`."""
    тело = _блок(nginx_conf, re.compile(rf"^map\s+\$http_authorization\s+{re.escape(КЛЮЧ)}\s*\{{"))
    assert тело, f"в nginx.conf нет `map $http_authorization {КЛЮЧ}` — ключ зоны api не из токена"
    return тело


def _regex_из_карты(карта: list[str]) -> tuple[str, str]:
    """(pattern, имя переменной-значения) из единственной regex-строки map."""
    строки = [
        m for line in карта if (m := re.match(r'^"~(?P<pat>[^"]+)"\s+\$(?P<var>\w+);$', line))
    ]
    assert len(строки) == 1, (
        f"в map ожидается ровно одна regex-строка по токену, нашлось {len(строки)}"
    )
    return строки[0]["pat"], строки[0]["var"]


# ============================================================ ключ зоны — человек


def test_зона_api_считает_по_ключу_человека(nginx_conf: list[str]) -> None:
    зона = _зона(nginx_conf, "api")
    assert зона, "в nginx.conf нет зоны `api` — location /api/ остался без лимита"
    assert зона.group(1) == КЛЮЧ, (
        f"зона api идёт по `{зона.group(1)}`, а не по `{КЛЮЧ}`: весь офис за одним NAT "
        "снова делит 30r/s на всех (06.09 — 344–364 отказа 429 в сутки)"
    )


def test_карта_без_токена_оставляет_адрес(карта: list[str]) -> None:
    """login, refresh по cookie и статика ходят без Bearer — им нужен прежний ключ."""
    assert any(re.match(r"^default\s+\$binary_remote_addr;$", line) for line in карта), (
        "в map нет `default $binary_remote_addr` — запросы без токена остались бы без учёта вовсе"
    )


def test_карта_берёт_хвост_bearer_токена(карта: list[str]) -> None:
    pattern, var = _regex_из_карты(карта)
    assert pattern.startswith("^Bearer "), f"regex map не привязан к `Bearer `: {pattern!r}"
    хвост = re.search(r"\(\?<(?P<name>\w+)>\.\{(?P<n>\d+)\}\)\$$", pattern)
    assert хвост, (
        f"regex map не вырезает хвост фиксированной длины в именованную группу: {pattern!r}"
    )
    assert хвост["name"] == var, (
        f"map отдаёт `${var}`, а regex ловит в `${хвост['name']}` — значение ключа было бы пустым, "
        "а пустой ключ nginx не считает вовсе"
    )


def test_хвост_из_map_вырезает_ровно_подпись_настоящего_токена(карта: list[str]) -> None:
    """Единственная проверка не по форме: regex прикладывается к живому токену.

    HS256 = HMAC-SHA256 = 32 байта → base64url без паддинга = 43 символа.
    Хвост длиннее подписи захватил бы точку и конец payload — у всех токенов
    одного человека payload разный (jti), так что ключ остался бы уникальным,
    но короче 32 символов хвост уже не разумен, а длиннее подписи — знак,
    что число в map никто не сверял с алгоритмом.
    """
    pattern, var = _regex_из_карты(карта)
    token = create_access_token(user_id="rl-guard", role="manager")
    подпись = token.split(".")[2]
    n = int(_группа(r"\.\{(\d+)\}\)\$$", pattern))
    assert 32 <= n <= len(подпись), (
        f"map берёт хвост в {n} символов, подпись токена — {len(подпись)}: "
        "хвост обязан лежать внутри подписи (уникальная часть) и быть не короче 32"
    )
    # nginx — PCRE, для такого шаблона отличие от re одно: синтаксис именованной группы
    m = re.search(pattern.replace("(?<", "(?P<"), f"Bearer {token}")
    assert m, "regex map не совпал с настоящим токеном — ключом стал бы адрес, тихо"
    assert m.group(var) == подпись[-n:], "regex map вырезал не хвост подписи"


# ============================================================ страховка по адресу


def test_страховочная_зона_по_адресу_стоит_поверх(nginx_conf: list[str], vhost: list[str]) -> None:
    личная = _зона(nginx_conf, "api")
    адресная = _зона(nginx_conf, "api_addr")
    assert личная and адресная, "нет зоны api или api_addr в nginx.conf"
    assert адресная.group(1) == "$binary_remote_addr", "зона api_addr обязана считать по адресу"
    assert int(адресная.group(3)) > int(личная.group(3)), (
        "потолок api_addr не выше личного — страховка душит офис раньше, чем личный лимит"
    )

    api_location = _блок(vhost, re.compile(r"^location\s+/api/\s*\{"))
    assert api_location, "в шаблоне нет `location /api/`"
    лимиты = [line for line in api_location if line.startswith("limit_req ")]
    зоны = {_группа(r"zone=(\w+)", line) for line in лимиты}
    assert зоны == {"api", "api_addr"}, (
        f"в location /api/ лимиты по зонам {sorted(зоны)}, нужны ровно api и api_addr: "
        "без api_addr мусорный Bearer даёт свежий ключ на каждый запрос и обходит лимит целиком"
    )
    burst_по_зоне = {
        _группа(r"zone=(\w+)", line): int(_группа(r"burst=(\d+)", line)) for line in лимиты
    }
    assert burst_по_зоне["api_addr"] > burst_по_зоне["api"], "burst страховки не выше личного"


def test_зону_api_использует_только_общий_location(vhost: list[str]) -> None:
    """Логин и вебхуки — по своим зонам и по адресу: у них токена нет."""
    строки = [line for line in vhost if re.search(r"limit_req\s+zone=api[\s;]", line)]
    assert len(строки) == 1, (
        f"zone=api встречается {len(строки)} раз(а), ожидался один location /api/"
    )


# ============================================================ ключ — секрет


def test_ключ_не_попадает_в_журнал(nginx_conf: list[str], vhost: list[str]) -> None:
    """Хвост подписи — секрет: с ним и адресом можно подобрать остальное дешевле.

    В access_log его нет, формат журнала не менялся; сам nginx при
    срабатывании пишет в error.log имя зоны и адрес, ключ — нет.
    """
    секреты = ("$lc_rl_key", "$lc_rl_sig", "$http_authorization")
    # log_format — многострочная директива без скобок: от слова до первой `;`
    log_formats = re.findall(
        r"^\s*log_format\b.*?;", NGINX_CONF.read_text(encoding="utf-8"), re.S | re.M
    )
    assert log_formats, "в nginx.conf не нашлось log_format — сторожу нечего проверять"
    for fmt in log_formats:
        for s in секреты:
            assert s not in fmt, f"`{s}` в log_format — кусок подписи уехал бы в журнал"

    for line in vhost:
        for s in секреты:
            assert s not in line, f"`{s}` в шаблоне vhost — ему там нечего делать: {line}"

    # в nginx.conf ключу разрешены ровно два места: сама map и строка зоны
    вне_карты = [
        line
        for line in nginx_conf
        if any(s in line for s in секреты)
        and not line.startswith(("map ", "limit_req_zone "))
        and not re.match(r'^(default|"~)', line)
    ]
    assert not вне_карты, f"ключ зоны всплыл вне map и limit_req_zone: {вне_карты}"
