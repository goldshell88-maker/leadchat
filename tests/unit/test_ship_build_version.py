"""Версия сборки обязана попадать в бандл фронта (аудит 22.08, находка 1).

ЧТО БЫЛО СЛОМАНО. `deploy/workstation/ship.sh` собирал образ веба строкой
`docker build -q -t <тег> -f docker/Dockerfile.web .` — без `--build-arg`.
В `docker/Dockerfile.web` умолчание `ARG VITE_APP_VERSION="dev"`, и vite
инлайнил в бандл именно заглушку. А `isStaleTab()` на заглушке молчит по
построению (`frontend/src/platform/buildVersion.ts`):

    if (server === PLACEHOLDER || BUILD_VERSION === PLACEHOLDER) return false;

То есть предупреждение «вкладка работает на старой сборке» было написано,
покрыто тестом `staleBuild.test.tsx` и ВЫКЛЮЧЕНО одной недостающей строкой
выкатки. Цена — не косметическая: `/assets/` отдаётся с `immutable` и
`try_files $uri =404`, поэтому после выкатки старые чанки исчезают с сервера,
и вкладка, открытая до неё, ломается на первом же переходе в ленивый раздел.
Выкатки идут в рабочий день на живой переписке.

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ. Локальная сборка тоже даёт «dev», и красным нигде не
становится: расхождение видно только на проде и только тому, кто сравнит бандл
с `/api/health` руками.

ЗДЕСЬ ПРОВЕРЯЕТСЯ КОНСТРУКЦИЯ СКРИПТА, а не его текст целиком: что образ веба
собирается с передачей версии и что версия — тот же коммит, который уходит в
`APP_VERSION` контейнеров. Иначе номера разойдутся и баннер начнёт врать.
"""

from __future__ import annotations

import pathlib
import re

SHIP = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "workstation" / "ship.sh"
DOCKERFILE = pathlib.Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.web"


def _текст() -> str:
    return SHIP.read_text(encoding="utf-8")


def test_образ_веба_собирается_с_версией():
    строки = [s for s in _текст().splitlines() if "local/leadchat-web" in s and "|" in s]
    assert строки, "в BUILD_IMAGES нет образа веба"
    web = строки[0]
    assert "--build-arg VITE_APP_VERSION=" in web, (
        "образ веба собирается без версии — в бандл уедет заглушка «dev», "
        "и предупреждение о старой вкладке не сработает никогда"
    )


def test_версия_бандла_это_выкатываемый_коммит():
    """Тот же тег, что уходит в APP_VERSION контейнеров, иначе баннер соврёт."""
    web = next(s for s in _текст().splitlines() if "local/leadchat-web" in s and "|" in s)
    m = re.search(r"--build-arg VITE_APP_VERSION=\$\{?(\w+)\}?", web)
    assert m, "версия передана не переменной — подставлять руками нельзя"
    assert m.group(1) == "COMMIT", (
        f"версия бандла берётся из ${m.group(1)}, а контейнеры получают $COMMIT — "
        "номера разойдутся, и вкладка будет считать себя устаревшей всегда"
    )


def test_коммит_известен_до_сборки():
    """COMMIT обязан быть определён РАНЬШЕ BUILD_IMAGES, иначе подставится пусто."""
    текст = _текст()
    assert текст.index('COMMIT="$(git rev-parse') < текст.index("BUILD_IMAGES=(")


def test_умолчание_в_dockerfile_осталось_заглушкой():
    """Умолчание менять НЕЛЬЗЯ: локальная сборка обязана давать «dev», иначе на
    стенде повиснет вечный баннер, который не гасится перезагрузкой."""
    assert 'ARG VITE_APP_VERSION="dev"' in DOCKERFILE.read_text(encoding="utf-8")
