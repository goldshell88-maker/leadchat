"""Сомнение «часть могла уйти» ставится на все обрывы ПОСЛЕ отправки (аудит 30.08).

⚠ ЧЕМ ЭТО ОПЛАЧИВАЕТСЯ. Признак `after_send` решает, поставит ли доставка ключ
сомнения. С ним следующая попытка сверяет хвост чата и не шлёт клиенту второй
раз то, что уже дошло (L-007). Без него повтор идёт вслепую — и клиент получает
одно и то же сообщение дважды. Отозвать его в Авито нельзя.

ЧТО БЫЛО. Признак звучал «таймаут, кроме таймаута соединения». Мимо проходили
`ReadError` и `RemoteProtocolError` — обрыв ПОСЛЕ того, как тело запроса ушло,
на чтении ответа; прокси Авито рвёт так регулярно. Вся защита L-007 покрывала
только таймауты.

ПРАВИЛО ТЕПЕРЬ ПО СУТИ: «тело запроса успело уйти в сокет». Осторожная сторона —
считать, что ушёл: лишняя сверка хвоста стоит одного запроса, пропущенная —
второго сообщения клиенту.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.avito.errors import AvitoUnavailable


def признак(exc: Exception) -> bool:
    """Тот же расчёт, что в клиенте: вынесен сюда ради читаемости таблицы."""
    НЕ_УШЁЛ = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.WriteError,
        httpx.WriteTimeout,
        httpx.UnsupportedProtocol,
        httpx.InvalidURL,
    )
    return not isinstance(exc, НЕ_УШЁЛ)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("оборвано на чтении ответа"),
        httpx.RemoteProtocolError("сервер закрыл соединение"),
        httpx.ReadTimeout("ответа не дождались"),
        httpx.PoolTimeout("пул занят"),
    ],
    ids=["ReadError", "RemoteProtocolError", "ReadTimeout", "PoolTimeout"],
)
def test_обрыв_после_отправки_считается_ушедшим(exc: Exception) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: два первых класса и были дырой."""
    assert признак(exc) is True, (
        f"{type(exc).__name__} не считается ушедшим — повтор пошлёт клиенту дубль"
    )


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("соединение не установилось"),
        httpx.ConnectTimeout("не достучались"),
        httpx.WriteError("оборвано на записи тела"),
        httpx.WriteTimeout("тело не ушло"),
    ],
    ids=["ConnectError", "ConnectTimeout", "WriteError", "WriteTimeout"],
)
def test_до_отправки_сомнения_нет(exc: Exception) -> None:
    """Иначе каждая недоступность Авито стоила бы лишней сверки хвоста чата."""
    assert признак(exc) is False, f"{type(exc).__name__} зря помечен как ушедший"


def test_расчёт_в_клиенте_совпадает_с_этим() -> None:
    """⚠ БЕЗ ЭТОГО ПРОВЕРКИ ВЫШЕ СТОРОЖИЛИ БЫ КОПИЮ ПРАВИЛА, А НЕ ПРАВИЛО.

    Таблица наверху повторяет расчёт клиента; разъедься они — тесты остались бы
    зелёными на любом дефекте. Сверяем состав перечня по исходнику.
    """
    import pathlib

    корень = pathlib.Path(__file__).resolve().parents[2]
    текст = (корень / "app" / "integrations" / "avito" / "client.py").read_text(encoding="utf-8")
    for имя in ("ConnectError", "ConnectTimeout", "WriteError", "WriteTimeout"):
        assert f"httpx.{имя}" in текст, f"{имя} пропал из перечня «не ушло»"
    assert "ушёл = not isinstance(exc, НЕ_УШЁЛ)" in текст, (
        "расчёт признака изменился — таблица выше больше ничего не сторожит"
    )


def test_признак_доезжает_до_исключения() -> None:
    """Он и есть то, что читает доставка."""
    assert AvitoUnavailable(after_send=True).after_send is True
    assert AvitoUnavailable(after_send=False).after_send is False
