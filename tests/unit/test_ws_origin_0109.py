"""Чужая страница не открывает наш сокет (аудит безопасности 01.09).

⚠ ЭТО ВТОРАЯ ЛИНИЯ, А НЕ ПЕРВАЯ, И ЭТО ВАЖНО ПОНИМАТЬ ПРАВИЛЬНО.

Захват сокета с чужого сайта (CSWSH) закрыт одноразовым тикетом: чтобы его
получить, нужен авторизованный запрос с токеном, которого у чужой страницы нет.
Проверка Origin добавлена на случай, если выдачу тикета когда-нибудь смягчат, —
она стоит три строки и закрывает целый класс будущих ошибок.

⚠ ПОЧЕМУ ОТСУТСТВУЮЩИЙ ORIGIN ПРОПУСКАЕТСЯ. Браузер шлёт этот заголовок при
рукопожатии ВСЕГДА, поэтому чужая страница обойти проверку молчанием не может. А
служебные клиенты (проверки выкатки SM-4/SM-5, наблюдатель) ходят без него —
отвергни мы их, и красным стал бы smoke, а не нападающий.

⚠ ПОЧЕМУ В СПИСКЕ TAURI. Десктопное приложение грузит фронт ЛОКАЛЬНО, и его
origin не наш домен (`WsClient.ts`: «в Tauri origin = tauri://localhost»).
Забудь про это — и первый же релиз десктопа остался бы без реального времени, а
выглядело бы это как «у них не работает интернет». Инсталлятор сейчас не выложен
(проверено: /download отдаёт 404), то есть сегодня это никого не задевает, — тем
легче про него забыть, и тем нужнее этот тест.
"""

from types import SimpleNamespace

import pytest

from app.api.routes.ws import свой_источник
from app.core.config import settings

pytestmark = pytest.mark.anyio


def _ws(**headers: str) -> SimpleNamespace:
    return SimpleNamespace(headers={k.replace("_", "-"): v for k, v in headers.items()})


def test_чужой_сайт_отвергается() -> None:
    ws = _ws(origin="https://evil.example", host="chat.example.ru")
    assert свой_источник(ws) is False, (
        "страница постороннего сайта открывает наш сокет — это и есть CSWSH"
    )


def test_свой_домен_пропускается() -> None:
    assert свой_источник(_ws(origin="https://chat.example.ru", host="chat.example.ru")) is True


def test_свой_домен_с_косой_чертой_пропускается() -> None:
    """Некоторые клиенты шлют origin с хвостовой чертой — это тот же источник."""
    assert свой_источник(_ws(origin="https://chat.example.ru/", host="chat.example.ru")) is True


def test_без_origin_пропускается() -> None:
    """Не браузер: проверки выкатки, наблюдатель, служебный клиент.

    Браузер заголовок шлёт всегда, поэтому обойти проверку молчанием нельзя.
    """
    assert свой_источник(_ws(host="chat.example.ru")) is True


@pytest.mark.parametrize(
    "origin",
    ["tauri://localhost", "https://tauri.localhost", "http://tauri.localhost"],
)
def test_десктоп_пропускается(origin: str) -> None:
    """Все три формы, которыми Tauri представляется на разных системах.

    Список берётся из настройки `ws_extra_origins`, а не зашит в код: домен
    меняется, способы упаковки десктопа тоже, и правка такого списка не должна
    требовать выкатки кода.
    """
    assert origin in settings.ws_extra_origins_list
    assert свой_источник(_ws(origin=origin, host="chat.example.ru")) is True


def test_похожий_домен_не_проходит() -> None:
    """`chat.example.ru.evil.com` — не наш домен, хотя и начинается похоже."""
    ws = _ws(origin="https://chat.example.ru.evil.com", host="chat.example.ru")
    assert свой_источник(ws) is False
