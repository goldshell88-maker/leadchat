"""Куда целятся выкатка и проверка после неё.

ЧТО СЛУЧИЛОСЬ. Во всех скриптах деплоя адресом по умолчанию стоял
`chat.partner-lead-centre.ru`. Домен ведёт НЕ на прод: его A-запись указывает
на <сторонний сервер>, где работает FreeScout — другой хелпдеск, с собственным
сертификатом Let's Encrypt на это же имя. То есть адрес не «висит в воздухе»,
а честно отвечает чужой системой, и ошибку не видно по коду ответа.

ЧЕМ ЭТО ГРОЗИЛО. Проверка после выкатки ходила бы к FreeScout, не находила
там `/api/health` и откатывала совершенно исправный релиз. Сборка десктопа
строила бы ссылку на обновление от чужого адреса — установленные приложения
пошли бы качать чужой файл. Certbot просил бы сертификат на имя, которое
резолвится в чужой сервер.

ЧТО СТЕРЕЖЁМ. Умолчания обязаны указывать на адрес, по которому система
доступна ФАКТИЧЕСКИ. Когда домен направят на прод, меняется одна константа
здесь и переменные окружения на сервере — а не восемь мест в скриптах.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Адрес, по которому LeadChat доступен снаружи сегодня.
LIVE_HOST = "188-225-34-82.sslip.io"

# Домен, который проекту обещан, но пока занят другой системой. В умолчаниях
# его быть не должно; в комментариях и документации — сколько угодно.
NOT_OURS_YET = "chat.partner-lead-centre.ru"

TARGETS = [
    "deploy/deploy.sh",
    "deploy/smoke.sh",
    "deploy/publish-desktop.sh",
    ".github/workflows/deploy.yml",
    ".github/workflows/desktop-release.yml",
    # Makefile сторож не покрывал, и ровно поэтому `make smoke` ещё сутки
    # ходил на чужой хелпдеск после того, как остальные пять мест починили.
    # Дыра в стороже дороже дыры, которую он сторожит: она создаёт уверенность.
    "Makefile",
]

# Каталог, в котором система стоит НА САМОМ ДЕЛЕ. Умолчание `/opt/leadchat`
# осталось от первой прикидки; на боевом сервере этого каталога нет вовсе, и
# `make prod-logs` молча уходил в пустоту. Проверяется отдельно от адреса:
# промах по каталогу так же не виден по коду ответа, как промах по домену.
LIVE_DIR = "/srv/leadchat"
STALE_DIR = "/opt/leadchat"


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Строки без комментариев: `#` в начале — пояснение, а не настройка."""
    out = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        out.append((i, raw))
    return out


@pytest.mark.parametrize("rel", TARGETS)
def test_no_script_defaults_to_the_domain_we_do_not_own(rel: str):
    path = ROOT / rel
    assert path.is_file(), f"{rel} исчез — сторож проверяет несуществующий файл"

    guilty = [(n, line.strip()) for n, line in _code_lines(path) if NOT_OURS_YET in line]
    assert not guilty, (
        f"{rel}: умолчание указывает на {NOT_OURS_YET}, а там чужая система "
        f"(FreeScout). Строки: {guilty}"
    )


def test_the_live_address_is_named_where_it_matters():
    """Обратная сторона: сторож не должен зеленеть на пустом месте.

    Если из скриптов пропадёт и адрес тоже, первая проверка пройдёт — просто
    потому, что искать стало нечего.
    """
    named = {rel for rel in TARGETS if LIVE_HOST in (ROOT / rel).read_text(encoding="utf-8")}
    missing = set(TARGETS) - named
    assert not missing, f"адрес прода назван не везде: не хватает в {missing}"


def test_the_release_gate_runs_in_strict_mode():
    """Гейт, который пропускает при нехватке данных, — не гейт (07 §7).

    Без `--strict` проверка с незаданными кредами помечается SKIP и на код
    возврата не влияет: забыли секрет — из десяти проверок реально прошла
    одна (что снаружи вообще кто-то отвечает), а выкатка засчиталась удачной.
    """
    deploy_yml = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    # Именно ЗАПУСК, а не любое упоминание: тем же именем скрипт копируется на
    # сервер в списке файлов, и «--strict» в той строке был бы бессмыслицей.
    smoke_calls = [ln.strip() for ln in deploy_yml.splitlines() if "bash deploy/smoke.sh" in ln]
    assert smoke_calls, "вызов smoke.sh исчез из конвейера — проверять выкатку стало нечем"
    for call in smoke_calls:
        assert "--strict" in call, f"smoke без --strict: {call}"


@pytest.mark.parametrize(
    "rel",
    [
        "Makefile",
        "deploy/deploy.sh",
        "deploy/backup.sh",
        "deploy/backup-verify.sh",
        "deploy/healthcheck-alert.sh",
        "deploy/restore-check.sh",
        "deploy/rollback.sh",
    ],
)
def test_no_stale_deploy_directory_in_defaults(rel: str):
    """Каталог по умолчанию — тот, в котором система стоит.

    Сегодня скрипты выживают только потому, что crontab на сервере задаёт
    DEPLOY_DIR явно. Человек, запустивший их руками в момент аварии, этой
    подстраховки не получит: `make prod-logs` уйдёт в несуществующий
    /opt/leadchat и покажет пустоту вместо логов.
    """
    text = (ROOT / rel).read_text(encoding="utf-8")
    offenders = [
        f"{rel}:{i}: {ln.strip()}"
        for i, ln in enumerate(text.splitlines(), 1)
        if STALE_DIR in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, "устаревший каталог в умолчаниях:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
#  Выкатка обязана собирать ВСЕ образы, которые сама же и запускает
# ---------------------------------------------------------------------------


def _compose_local_images() -> set[str]:
    """Образы `local/…` из обоих файлов compose — их никто не тянет из реестра."""
    images: set[str] = set()
    for name in ("docker-compose.prod.yml", "docker-compose.override.yml"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("image:") and "local/" in stripped:
                images.add(stripped.split("image:", 1)[1].strip())
    return images


def test_ship_builds_every_image_it_runs():
    """Каждый локально собираемый образ обязан быть в списке сборки.

    ЧТО СЛУЧИЛОСЬ. Шаг сборки собирал ровно два образа — api и web. Имитатор
    Авито `fake-avito` не собирался никогда. Он на любые ключи отдавал один и
    тот же выдуманный аккаунт; в репозитории это починили, выкатка прошла
    зелёной, контейнер перезапустился — но из СТАРОГО образа. Снаружи система
    выглядела исправной, а владелец сутки не мог подключить ни одного аккаунта
    и писал «ничего не привязывается».

    Поймать это было нечем: проверка здоровья отвечает «ок», логи чистые,
    а расхождение между репозиторием и работающим контейнером не видно ниоткуда.
    """
    ship = (ROOT / "deploy/workstation/ship.sh").read_text(encoding="utf-8")
    declared = _compose_local_images()
    assert declared, "в compose не нашлось ни одного образа local/… — проверь разбор файла"

    missing = [img for img in sorted(declared) if f'"{img}|' not in ship]
    assert not missing, (
        "выкатка не собирает образы, которые сама запускает: "
        + ", ".join(missing)
        + " — допишите их в BUILD_IMAGES в deploy/workstation/ship.sh"
    )


def test_ship_refuses_to_deploy_an_image_it_did_not_build():
    """Список сборки обязан сверяться с compose НА САМОЙ ВЫКАТКЕ, а не только здесь.

    Этот тест ловит расхождение в репозитории. Но compose на сервере может
    разойтись с репозиторием (например, файл правили руками), и тогда система
    снова поднимется на образе, которого никто не собирал. Поэтому сверка
    обязана быть и в самом скрипте — до перезапуска, а не после.
    """
    ship = (ROOT / "deploy/workstation/ship.sh").read_text(encoding="utf-8")
    assert "BUILD_IMAGES" in ship
    assert "объявлен в compose, но выкатка его не собирает" in ship, (
        "в ship.sh нет стража «образ объявлен, но не собирается» — верните его"
    )


# --- секреты на сервере (#36) ------------------------------------------------


def test_ship_checks_server_secrets_before_touching_anything():
    """Гейт секретов обязан стоять В САМОЙ ВЫКАТКЕ и ДО первого действия.

    Валидатор в Settings ловит заглушку на каждом старте контейнера, но `.env`
    на сервере правят и после выкатки — руками, в спешке, при разборе аварии.
    А сама выкатка до сих пор не смотрела на переменные окружения ВООБЩЕ.

    Порядок проверяется отдельно и не для красоты: после снимка базы и rsync
    это уже не гейт, а уборка последствий. Падение до первого действия не стоит
    ничего.
    """
    ship = (ROOT / "deploy/workstation/ship.sh").read_text(encoding="utf-8")

    assert "REQUIRED_SECRETS" in ship, "в ship.sh нет проверки секретов на сервере"
    assert "MEDIA_SIGN_KEY" in ship
    assert "строка-заглушка из репозитория" in ship, (
        "гейт обязан отличать заглушку от настоящего ключа: пустое значение ловит и compose, "
        "а непустой CHANGE_ME из .env.prod.example проходит насквозь"
    )
    assert "проверить секреты нечем" in ship, (
        "недоступный .env обязан валить выкатку отдельным сообщением: сторож, который "
        "зеленеет, ничего не увидев, хуже отсутствующего"
    )

    gate = ship.index("REQUIRED_SECRETS")
    backup = ship.index("deploy/backup.sh")
    assert gate < backup, (
        "проверка секретов стоит после снимка базы — к этому моменту выкатка уже началась"
    )


def test_the_signature_formula_matches_between_python_and_nginx():
    """Подпись ссылок считают ДВА разных кода, и они обязаны совпадать.

    В проде ссылки проверяет nginx (`secure_link_md5`), в разработке — Python.
    Формула строки подписи — контракт между ними, и держится он сегодня на
    комментарии. Расхождение даёт 403 на КАЖДОЕ вложение и не ловится ни
    health, ни smoke: снаружи система здорова, просто ни одна фотография
    клиента не открывается.
    """
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    from app.core.config import settings
    from app.services import media as media_svc

    nginx = (ROOT / "docker/nginx/templates/leadchat.conf.template").read_text(encoding="utf-8")
    # nginx: secure_link_md5 "$secure_link_expires$uri ${MEDIA_SIGN_KEY}" — и у своих
    # вложений, и у снимков с CDN Авито (12.09) одна формула.
    assert nginx.count('secure_link_md5 "$secure_link_expires$uri ${MEDIA_SIGN_KEY}"') == 2, (
        "формула подписи в шаблоне nginx изменилась — сверьтесь с _signed_uri в media.py"
    )

    # Python: подпись ссылки обязана сходиться с формулой nginx, посчитанной здесь.
    def как_nginx(url: str) -> bool:
        части = urlsplit(url)
        параметры = parse_qs(части.query)
        exp, sig = параметры["exp"][0], параметры["sig"][0]
        raw = f"{exp}{части.path} {settings.media_sign_key}"
        ожидаем = base64.urlsafe_b64encode(hashlib.md5(raw.encode()).digest())  # noqa: S324
        return sig == ожидаем.rstrip(b"=").decode()

    assert как_nginx(media_svc.signed_media_url("2026/09/12/a.jpg"))
    assert как_nginx(media_svc.proxied_avito_image_url("https://40.img.avito.st/image/1/a.jpg"))


# --- запасной вход (#17) ------------------------------------------------------


def test_the_desktop_app_knows_the_fallback_address_in_advance():
    """Адрес запасного входа обязан быть в приложении ДО аварии.

    ПОЧЕМУ ЭТО НЕЛЬЗЯ ДОБАВИТЬ ПОТОМ. Настольное приложение обновляется,
    скачивая новую версию с сервера. Если лёг тот самый сервер, обновление
    взять неоткуда — а значит и адрес запасного входа в приложение уже не
    попадёт. Единственный момент, когда его можно вписать, — заранее.

    Мест два, и оба обязательны. Список разрешённых адресов (CSP) — иначе
    приложение откажется соединяться, даже зная адрес. Список адресов
    обновления — иначе после переезда на запасной сервер команда останется без
    обновлений навсегда.

    ЧТО ЭТА ПРОВЕРКА НЕ ЗНАЧИТ. Она не говорит, что запасной вход работает:
    зеркала системы для диспетчеров нет, амстердамский сервер сегодня —
    хранилище резервных копий и транзит для разработчика. Она запирает ровно
    одно: возможность им когда-нибудь воспользоваться не потеряна.
    """
    import json

    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))

    csp = conf["app"]["security"]["csp"]
    connect = csp.split("connect-src", 1)[1].split(";", 1)[0]
    assert "72-56-68-159" in connect, (
        "в CSP нет адреса запасного входа — приложение не сможет к нему подключиться, "
        "а дописать его после аварии будет уже неоткуда"
    )

    endpoints = conf["plugins"]["updater"]["endpoints"]
    assert any("72-56-68-159" in url for url in endpoints), (
        "в списке адресов обновления нет запасного — переехав на него, команда останется "
        "без обновлений навсегда"
    )
    assert "72-56-68-159" in endpoints[-1], (
        "запасной адрес обязан стоять ПОСЛЕДНИМ: он проверяется, только когда основные "
        "не ответили, и не должен замедлять обычное обновление"
    )


def test_ship_runs_the_regression_and_does_not_hide_skips():
    """Выкатка обязана гонять регрессию, а не только пинговать здоровье.

    ЗДЕСЬ СКРИПТ ЗАКАНЧИВАЛСЯ пингом `/api/health` — и печатал «готово».
    А health отвечает «ok», пока живы процесс, база и Redis: он ничего не
    знает ни про вход в систему, ни про то, разбирает ли воркер очередь, ни
    про то, отдаётся ли статика. Набор проверок для этого написан и лежал в
    репозитории неиспользованным.

    ПРО ПРОПУСКИ отдельно. Половина набора требует служебной учётки, которой
    на сервере нет, и её проверки помечаются SKIP. Строка «5 pass, 0 fail,
    5 skip» выглядит зелёной — именно так это и осталось незамеченным.
    Пропущенная проверка это непроверенное место, а не пройденное, и выкатка
    обязана говорить об этом отдельно.
    """
    ship = (ROOT / "deploy/workstation/ship.sh").read_text(encoding="utf-8")
    assert "deploy/smoke.sh" in ship, "выкатка не гоняет регрессию"
    assert "НЕПРОВЕРЕННЫЕ места" in ship, (
        "выкатка молчит про пропущенные проверки — а зелёная строка с пропусками читается как успех"
    )
    health = ship.index("api/health")
    smoke = ship.index("deploy/smoke.sh")
    assert health < smoke, "регрессия обязана идти ПОСЛЕ подъёма и проверки здоровья"
