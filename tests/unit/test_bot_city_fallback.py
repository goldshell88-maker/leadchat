"""Город для бота берётся из ссылки, когда слага нет.

ЗАМЕР БОЕВОЙ БАЗЫ 26.08. `conversations.item_city_slug` пуст у 4832 диалогов из
15 970 — это 30 %. Ссылка на объявление есть у 4710 из них (97,5 %), и город лежит
прямо в ней: `avito.ru/tuapse/…`, `avito.ru/surgut/…`, `avito.ru/yaroslavl/…`.

ПОЧЕМУ ЭТО ДОРОГО ИМЕННО У БОТА. Без города он не имеет права называть окно приезда:
«час-полтора» верно только в черте города, а где клиент — неизвестно. Окно же первым
ходом — сильнейший ход воронки: 26,0 % до телефона против 11,7 % у хода без шага
(23 159 первых ходов операторов, analysis/move_value.py в репозитории лид-бота).
То есть на каждом третьем диалоге бот молчал о времени на ровном месте.

⚠ ЗАПАСНОЙ ПУТЬ БЫЛ ВЕЗДЕ, КРОМЕ БОТА. `services/conversations.py` давно читает
`conv.item_city_slug or parse_listing_url(conv.item_url).city_slug`. Единственным
местом без него оказался путь с самой высокой ценой ошибки.

⚠ МЕСТ ОКАЗАЛОСЬ ДВА (28.08). Этот сторож проверял бота — и на нём остановился.
Второе место нашлось в выдаче заявок (`services/leads.py`): там город читался
только из колонки, и при пустой колонке заявка молча уходила в придержку «город
не распознан». По тому же замеру это почти треть заявок — и все они не уезжали в
лид-центр, хотя город лежал в ссылке, а диспетчер видел его чипом в шапке чата.
Ниже добавлена проверка по СПИСКУ мест: сторож, знающий одно место, ловит один
случай.
"""

from __future__ import annotations

import pytest

from app.bots.engine import город_диалога


class _Беседа:
    def __init__(self, slug: str | None, url: str | None) -> None:
        self.item_city_slug = slug
        self.item_url = url


def _город(slug: str | None, url: str | None) -> str:
    """Зовём БОЕВУЮ функцию, а не её копию: копия разошлась бы на первой правке."""
    return город_диалога(_Беседа(slug, url))


@pytest.mark.parametrize(
    ("url", "ждём"),
    [
        ("https://avito.ru/tuapse/predlozheniya_uslug/muzh_na_chas_8100504000", "Туапсе"),
        ("https://avito.ru/yaroslavl/predlozheniya_uslug/telemaster_8159673720", "Ярославль"),
        ("https://avito.ru/surgut/predlozheniya_uslug/remont_televizorov_8307092932", "Сургут"),
        ("https://www.avito.ru/moskva/predlozheniya_uslug/sborka_mebeli_8213779975", "Москва"),
    ],
)
def test_город_достаётся_из_ссылки_когда_слага_нет(url: str, ждём: str) -> None:
    assert _город(None, url) == ждём


def test_слаг_имеет_приоритет_над_ссылкой() -> None:
    """Слаг — снимок на момент обращения, ссылка могла переехать (docs/32)."""
    assert (
        _город("sankt-peterburg", "https://avito.ru/tuapse/predlozheniya_uslug/x_8100504000")
        == "Санкт-Петербург"
    )


def test_нет_ни_того_ни_другого_город_пустой() -> None:
    assert _город(None, None) == ""
    assert _город(None, "мусор") == ""


def test_чужой_домен_городом_не_становится() -> None:
    """avito.ru.evil.com не должен давать город: ссылка не наша."""
    assert _город(None, "https://avito.ru.evil.com/moskva/predlozheniya_uslug/x_8100504000") == ""


# ------------------------------------------- запасной путь во ВСЕХ местах


def test_запасной_путь_есть_у_каждого_потребителя_города() -> None:
    """Кто спрашивает город диалога — обязан спрашивать и ссылку.

    Сторож по СПИСКУ, а не по одному месту: первая редакция знала только бота, и
    выдача заявок с тем же промахом прожила рядом две недели.

    Проверяем по дереву разбора, а не по строке: `ruff format` переносит длинные
    выражения, и проверка по тексту краснела бы от форматирования.
    """
    import ast
    import inspect
    import textwrap

    from app.services import conversations as convs_mod
    from app.services import leads as leads_mod

    # С 11.09 карточка объявления и проверка адреса по карте берут город ОДНИМ
    # путём — `conversation_city_slug`; запасной путь по ссылке живёт в нём.
    ПОТРЕБИТЕЛИ = [
        (convs_mod.conversation_city_slug, "город диалога: карточка объявления и карта"),
        (leads_mod.collect, "выдача заявок в лид-центр"),
    ]
    from app.bots import engine as engine_mod

    ПОТРЕБИТЕЛИ.append((engine_mod.город_диалога, "окно приезда у бота"))

    for функция, зачем in ПОТРЕБИТЕЛИ:
        дерево = ast.parse(textwrap.dedent(inspect.getsource(функция)))
        имена = {
            getattr(у.func, "id", None) or getattr(у.func, "attr", None)
            for у in ast.walk(дерево)
            if isinstance(у, ast.Call)
        }
        assert "city_by_slug" in имена or "parse_listing_url" in имена, (
            f"{функция.__name__} ({зачем}) больше не определяет город — обнови список"
        )
        assert "parse_listing_url" in имена, (
            f"{функция.__name__} ({зачем}): город берётся только из колонки. "
            "По замеру 26.08 она пуста у 30 % диалогов, и у 97,5 % из них город "
            "лежит в ссылке объявления"
        )


def test_карточка_объявления_и_карта_берут_город_одним_путём() -> None:
    """Чип «город Орск» и город, с которым идут к карте, обязаны быть одним
    городом: иначе оператор видит один, а под адресом читает «найден в другом»."""
    import ast
    import inspect
    import textwrap

    from app.services import conversations as convs_mod

    for функция in (convs_mod.item_out, convs_mod.conversation_city):
        дерево = ast.parse(textwrap.dedent(inspect.getsource(функция)))
        имена = {
            getattr(у.func, "id", None) or getattr(у.func, "attr", None)
            for у in ast.walk(дерево)
            if isinstance(у, ast.Call)
        }
        assert "conversation_city_slug" in имена, функция.__name__
