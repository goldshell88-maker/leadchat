"""Ангарск нумерует и микрорайоны, и кварталы: тип массива решает номер (25.09).

Тройка и пара без слова типа в Ангарске шли одним «кварталом». Карта же
знает там оба массива: замер боя 25.09 по 139 адресам, которые она
подтвердила, — микрорайоны с номерами 6…33 (65 адресов), выше 33 — только
кварталы (67), кварталов с номером до 33 — 7, по одному на номер. «22 квартал,
5» карта не находит, и сторож типа улицы дом не подтверждает. Теперь малый
номер — «мкр», большой — «квартал»; слово клиента («93 кв,31») номер не
перебивает; Шелехов и Нефтеюганск — как были.

ДИВЕРСИИ (каждая обязана краснеть): убрать Ангарск из `MICRODISTRICTS_UP_TO`
→ «22 квартал» у тройки и пары; сдвинуть границу на 34 → «34 мкр»; не
передать границу разбору названного города → «Ангарск 22-5-8» с кварталом;
применить границу к «кв» клиента → «12 кв,5» становится «12 мкр».

Телефонов, имён и адресов клиентов в тестах нет: номера массивов и домов
вымышлены.
"""

from __future__ import annotations

import pytest

from app.integrations.avito.listing_url import City
from app.services import address_parse as ap
from app.services import geocode as g


def _street(found: ap.Found | None) -> str | None:
    return None if found is None else found.street


@pytest.mark.parametrize(
    ("text", "street"),
    [
        ("22-5-8", "22 мкр"),
        ("33-2-10", "33 мкр"),
        ("34-2-10", "34 квартал"),
        ("91-4-17", "91 квартал"),
        ("Адрес 12-3-40. Жду после обеда", "12 мкр"),
    ],
)
def test_angarsk_dashed_triple_type_follows_the_number(text: str, street: str) -> None:
    assert _street(ap.parse_by_city(text, "Ангарск")) == street


@pytest.mark.parametrize(
    ("text", "street"),
    [
        ("адрес 22-5", "22 мкр"),
        ("адрес 85-11", "85 квартал"),
    ],
)
def test_angarsk_pair_type_follows_the_number(text: str, street: str) -> None:
    assert _street(ap.parse_by_city(text, "Ангарск")) == street


def test_the_clients_own_quarter_word_is_kept() -> None:
    """«кв» у номера — квартал, названный клиентом: номер его не перебивает."""
    assert _street(ap.parse_by_city("12 кв,5", "Ангарск")) == "12 квартал"


@pytest.mark.parametrize(
    ("text", "street"),
    [
        ("Ангарск 22-5-8", "22 мкр"),
        ("Ангарск, 91-4-17", "91 квартал"),
    ],
)
def test_city_named_in_the_message_uses_the_same_boundary(text: str, street: str) -> None:
    assert _street(ap.parse(text)) == street


@pytest.mark.parametrize(
    ("city", "street"),
    [("Шелехов", "22 квартал"), ("Нефтеюганск", "22 мкр")],
)
def test_other_quarter_cities_are_unchanged(city: str, street: str) -> None:
    assert _street(ap.parse_by_city("22-5-8", city)) == street


def test_the_map_confirms_a_small_number_as_a_microdistrict() -> None:
    """Ответ карты «22-й микрорайон, 5» подтверждает «22 мкр» и не подтверждает
    «22 квартал», который разбор писал до 25.09. Ответ подставлен руками."""
    angarsk = City("Ангарск", "Иркутская область", "Asia/Irkutsk")
    answer = g.GeoHit(
        street="22-й микрорайон",
        house="5",
        settlement=None,
        city="Ангарск",
        region="Иркутская область",
        lat=52.5,
        lon=103.9,
        house_level=True,
    )

    def verdict(street: str) -> str:
        return g.verdict(g.Parsed(street=street, house="5"), angarsk, [answer])[0]

    found = ap.parse_by_city("22-5-8", "Ангарск")
    assert found is not None
    assert verdict(found.street) == g.GEO_EXACT
    assert verdict("22 квартал") == g.GEO_STREET_MISMATCH
