"""Ссылка на профиль клиента на Авито (просьба владельца 02.09).

⚠ ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ. Не «ссылка появилась», а «ссылка появляется ТОЛЬКО
настоящая». Пять месяцев на её месте стояла заглушка, и довод был не в лени:
собрать адрес было не из чего, а собранный догадкой ведёт в 404 — по такой
жмут при клиенте, с телефона. Заглушка сама назвала условие снятия: «после
того, как `client_enrich.peer_fields` покажет в логе боевое поле со ссылкой».
Показал 02.09 — поле `public_user_profile`.

Форма адреса внутри поля при этом первым лицом НЕ проверена: она взята из
разбора docs/44 §6 со ссылкой на спецификацию, которой в репозитории нет
(docs/30 §B1), и одну её часть наш собственный код уже опроверг. Поэтому разбор
на форму не полагается, а эти проверки стерегут именно отказ: что негодное
значение не станет ссылкой ни при какой форме.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.avito.adapter import AvitoAdapter, _peer_profile_url

НАСТОЯЩИЙ = "https://avito.ru/user/0a1b2c3d4e5f60718293a4b5c6d7e8f9/profile"


def профиль(значение: Any) -> dict[str, Any]:
    return {"id": 923456789, "name": "Иван", "public_user_profile": значение}


class ГодныеАдреса:
    """То, что обязано пройти."""

    @staticmethod
    @pytest.mark.parametrize(
        "поле",
        [
            pytest.param({"url": НАСТОЯЩИЙ}, id="словарь-с-url"),
            # Если Авито однажды отдаст адрес самим полем — возьмём и его:
            # проверка ниже одна и та же, лишней ветки риска нет.
            pytest.param(НАСТОЯЩИЙ, id="голая-строка"),
            pytest.param({"url": f"  {НАСТОЯЩИЙ}  "}, id="с-пробелами"),
        ],
    )
    def test_настоящий_адрес_проходит(поле: Any) -> None:
        assert _peer_profile_url(профиль(поле)) == НАСТОЯЩИЙ


@pytest.mark.parametrize(
    ("поле", "почему"),
    [
        pytest.param(None, "поля нет вовсе", id="нет-поля"),
        pytest.param({}, "словарь без url", id="пустой-словарь"),
        pytest.param({"url": ""}, "пустая строка", id="пустая-строка"),
        pytest.param({"url": 12345}, "число вместо адреса", id="число"),
        pytest.param({"avatar": {"default": "x"}}, "есть аватар, но не адрес", id="только-аватар"),
        # ⚠ СХЕМА. `javascript:` в браузере погасила бы CSP, но в настольном
        # приложении тот же адрес уходит в системный обработчик схемы.
        pytest.param({"url": "javascript:alert(1)"}, "не веб-схема", id="javascript"),
        pytest.param({"url": "data:text/html,x"}, "не веб-схема", id="data"),
        pytest.param({"url": "http://avito.ru/user/x/profile"}, "не https", id="http"),
        # ⚠ ПОДДЕЛКИ ХОСТА. Проверка «оканчивается на avito.ru» пропустила бы
        # первую, проверка «содержит avito.ru» — обе.
        pytest.param(
            {"url": "https://avito.ru.evil.com/user/x/profile"}, "чужой домен", id="суффикс"
        ),
        pytest.param({"url": "https://evil.com/avito.ru/user/x"}, "чужой домен", id="в-пути"),
        # Всё до `@` — это userinfo, настоящий хост здесь evil.com.
        pytest.param({"url": "https://avito.ru@evil.com/x"}, "хост за userinfo", id="userinfo"),
        pytest.param({"url": "https://[/"}, "адрес не разбирается", id="битый"),
    ],
)
def test_негодное_ссылкой_не_становится(поле: Any, почему: str) -> None:
    assert _peer_profile_url(профиль(поле)) is None, почему


def чат(users: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "u2i-1", "users": users, "updated": 1_756_000_000}


МЫ = {"id": 111222333, "name": "Наш аккаунт"}


def test_адрес_доезжает_до_chatinfo() -> None:
    """⚠ ПРОВОДКА. Разбор можно написать, покрыть тестами и не позвать ни разу.

    Именно это и случилось бы, забудь кто-нибудь строку `client_profile_url=`
    в конструкторе `ChatInfo`: поле молча стало бы вечным `None`, и ни один
    тест разбора этого бы не заметил.
    """
    info = AvitoAdapter.parse_chat(чат([МЫ, профиль({"url": НАСТОЯЩИЙ})]), account_user_id=МЫ["id"])
    assert info.client_external_id == "923456789"
    assert info.client_profile_url == НАСТОЯЩИЙ


def test_в_чате_на_троих_профиль_не_показываем() -> None:
    """⚠ «ПЕРВЫЙ НЕ НАШ» В ЧАТЕ НА ТРОИХ — СЛУЧАЙНЫЙ ЧЕЛОВЕК.

    Разбор берёт первого не-нашего и обрывает цикл; ради этой самой беды у
    `ChatInfo` заведено поле `peer_count`. Ошибиться подписью неприятно, а
    открыть диспетчеру ПРИ КЛИЕНТЕ профиль постороннего — вред другого разряда.
    """
    третий = {"id": 999888777, "name": "Кто-то ещё"}
    info = AvitoAdapter.parse_chat(
        чат([МЫ, профиль({"url": НАСТОЯЩИЙ}), третий]), account_user_id=МЫ["id"]
    )
    assert info.peer_count == 3
    assert info.client_profile_url is None
    # Имя при этом остаётся: сужать его — отдельное решение владельца, а не
    # попутная правка. Проверка стоит здесь, чтобы разница была намеренной.
    assert info.client_name == "Иван"


def test_форма_попадает_в_лог_а_человек_нет(monkeypatch: pytest.MonkeyPatch) -> None:
    """Скелет пути объясняет устройство ссылки, не выдавая ключа человека."""
    from app.integrations.avito import adapter as мод

    записи: list[dict[str, Any]] = []

    class Шпион:
        def info(self, event: str, **kw: Any) -> None:
            записи.append({"event": event, **kw})

        def debug(self, event: str, **kw: Any) -> None:
            записи.append({"event": event, **kw})

    monkeypatch.setattr(мод, "log", Шпион())
    monkeypatch.setattr(мод, "_ФОРМЫ_В_ЛОГЕ", set())

    # Отказ по чужому хосту — с ключом человека внутри адреса.
    _peer_profile_url(профиль({"url": "https://evil.com/user/0a1b2c3d4e5f60718293a4b5c6d7e8f9/x"}))

    assert записи, "форма обязана попасть в лог: иначе чинить будут вслепую"
    целиком = json.dumps(записи, ensure_ascii=False)
    assert "0a1b2c3d4e5f60718293a4b5c6d7e8f9" not in целиком, "ключ человека утёк в лог"
    assert "<hex:32>" in целиком, "скелет пути обязан показывать устройство ссылки"
    assert "evil.com" in целиком, "хост нужен, чтобы понять причину отказа"


def test_одна_и_та_же_форма_пишется_однажды(monkeypatch: pytest.MonkeyPatch) -> None:
    """`parse_chat` зовётся на каждом разборе чата — журнал залить нельзя."""
    from app.integrations.avito import adapter as мод

    записи: list[str] = []

    class Шпион:
        def info(self, event: str, **kw: Any) -> None:
            записи.append(event)

        def debug(self, event: str, **kw: Any) -> None:
            записи.append(event)

    monkeypatch.setattr(мод, "log", Шпион())
    monkeypatch.setattr(мод, "_ФОРМЫ_В_ЛОГЕ", set())

    for _ in range(50):
        _peer_profile_url(профиль({"url": "http://avito.ru/user/x/profile"}))

    assert len(записи) == 1, f"форма записана {len(записи)} раз вместо одного"
