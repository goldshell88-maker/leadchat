"""Вердикт по ответам карты — чистые проверки на записанных ответах.

⚠ «ГОРОД СОВПАЛ» — НЕ СТОРОЖ. Город мы сами кладём в запрос, и карта отвечает
домом в нём — любым. Каждый тест ниже — живой провал, названный спором
проекта 11.09: «Октября 5» карта подтверждала домом на «40 лет Октября»,
«Ленина 5» — первой из четырёх улиц Ленина округа, «12 корпус 2» — домом 12.

ДИВЕРСИИ (каждая обязана краснеть): убрать сравнение дома → «спросили 12к2,
вернули 12» зеленеет; убрать единственность → две улицы Ленина дают exact;
убрать улицу по словам → «40 лет Октября» на «Октября 5»; убрать город клиента
→ «Гай, Ленина 5» при объявлении Орска даёт exact; убрать регион → дом из
другой области.
"""

import pytest

from app.integrations.avito.listing_url import City
from app.services import geocode as g

ОРСК = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")
МОСКВА_МО = City("Москва и МО", "Москва и область", "Europe/Moscow")


def дом(**over) -> g.GeoHit:
    """Ответ Nominatim по форме записи 11.09; адрес и точка вымышленные."""
    base: dict[str, object] = {
        "street": "Звенигородская улица",
        "house": "1",
        "settlement": "Заречный",
        "city": "Орск",
        "region": "Оренбургская область",
        "lat": 51.2101,
        "lon": 58.5012,
        "house_level": True,
    }
    base.update(over)
    return g.GeoHit(**base)  # type: ignore[arg-type]


ЗАРЕЧНЫЙ = g.Parsed(
    street="ул звенигородская", house="1", settlement="заречный", settlement_type="посёлок"
)


# --- exact и формат ----------------------------------------------------------


def test_образец_владельца_подтверждается_и_форматируется_как_в_картах() -> None:
    статус, hit = g.verdict(ЗАРЕЧНЫЙ, ОРСК, [дом()])
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, ЗАРЕЧНЫЙ) == "Звенигородская улица, 1, посёлок Заречный, Орск"


def test_район_которого_клиент_не_называл_в_строку_не_идёт() -> None:
    """`suburb` у OSM — и посёлок, и городской район: «посёлок Форштадт» — выдумка,
    а «улица Ленина, 5, Форштадт, Орск» ложится в карточку (ревью 11.09)."""
    без_типа = g.Parsed(street="ул звенигородская", house="1")
    статус, hit = g.verdict(без_типа, ОРСК, [дом(settlement_kind="district")])
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, без_типа) == "Звенигородская улица, 1, Орск"
    # Отдельный населённый пункт (деревня, посёлок как place) — всегда, голым именем.
    деревня = дом(settlement="Васильково", settlement_kind="place")
    assert g.format_address(деревня, без_типа) == "Звенигородская улица, 1, Васильково, Орск"
    # Назвал клиент — идёт с его типом даже для района.
    статус, hit = g.verdict(ЗАРЕЧНЫЙ, ОРСК, [дом(settlement_kind="district")])
    assert g.format_address(hit, ЗАРЕЧНЫЙ) == "Звенигородская улица, 1, посёлок Заречный, Орск"


def test_пункт_равный_городу_не_повторяется() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    hit = дом(street="улица Ленина", house="5", settlement="Орск")
    assert g.format_address(hit, parsed) == "улица Ленина, 5, Орск"


def test_текст_в_карточку_собирается_одним_сборщиком_с_частями() -> None:
    """Кнопка «Подтвердить» и автозапись обязаны дать один текст.

    Строка карты — когда она есть: дом при `exact` и улица без дома при
    отказе (`geo_formatted` пишет воркер только при известной улице, 18.09);
    ворота «годна ли строка» держит `clients.candidate_address_text`, сюда
    без степени `geo_formatted` не попадает.
    """
    части = {"office": "3", "entrance": "2", "floor": None, "intercom": "1234"}
    assert (
        g.address_text(
            geo_formatted="Звенигородская улица, 1, Орск",
            geo_status="exact",
            value="ул звенигородская, 1",
            parts=части,
        )
        == "Звенигородская улица, 1, Орск, кв 3, подъезд 2, домофон 1234"
    )
    # Карта не подтвердила и строки улицы нет — в карточку идут слова
    # клиента, части при них.
    assert (
        g.address_text(
            geo_formatted=None,
            geo_status="street_mismatch",
            value="ул звенигородская, 1",
            parts=части,
        )
        == "ул звенигородская, 1, кв 3, подъезд 2, домофон 1234"
    )
    # ОЖИДАНИЕ ИЗМЕНЕНО 18.09 (контракт автопривязки, п. 2): у отказа по
    # содержанию строка карты — это строка улицы с номером клиента, которую
    # воркер пишет только под сторожем STREET_KNOWN (степень `text`); в
    # карточку идёт она, а не слова клиента. До 18.09 такая строка у отказа
    # не записывалась вовсе, и сборщик брал слова клиента.
    assert (
        g.address_text(
            geo_formatted="ул Звенигородская, 1, Орск",
            geo_status="house_missing",
            value="ул звенигородская, 1",
            parts=части,
        )
        == "ул Звенигородская, 1, Орск, кв 3, подъезд 2, домофон 1234"
    )


# --- именованные отказы -------------------------------------------------------


def test_дом_равен_после_нормализации_а_не_по_первым_цифрам() -> None:
    """Спросили «12 корпус 2», карта вернула дом 12 — это соседний дом."""
    parsed = g.Parsed(street="ул Ленина", house="12 корпус 2")
    assert (
        g.verdict(parsed, ОРСК, [дом(street="улица Ленина", house="12")])[0] == g.GEO_HOUSE_MISMATCH
    )
    assert g.verdict(parsed, ОРСК, [дом(street="улица Ленина", house="12к2")])[0] == g.GEO_EXACT
    assert g.verdict(parsed, ОРСК, [дом(street="улица Ленина", house="12 к2")])[0] == g.GEO_EXACT


def test_улица_карты_не_длиннее_слов_клиента() -> None:
    """Слово карты ПОСЛЕ совпавшего — другая улица: «Пушкина» ≠ «Пушкина Заречная».

    ⚠ ПРАВИЛО СМЯГЧЕНО 12.09 (замер по 60 отказам боя): слова ПЕРЕД фамилией
    люди опускают — «Дзержинского» это «Феликса Дзержинского», «Октября» —
    «40 лет Октября», если другой улицы Октября в городе карта не нашла. Две
    подошедшие улицы дают «неоднозначно» и варианты, а не тихую ошибку.
    """
    parsed = g.Parsed(street="Пушкина", house="5")
    hit = дом(street="улица Пушкина Заречная", house="5", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_STREET_MISMATCH
    # Слова перед фамилией — опущены, улица та же.
    короткое = g.Parsed(street="Октября", house="5")
    длинная = дом(street="улица 40 лет Октября", house="5", settlement=None)
    assert g.verdict(короткое, ОРСК, [длинная])[0] == g.GEO_EXACT
    # Две улицы подошли — варианты, а не угадывание.
    другая = дом(street="улица Октября", house="5", settlement=None, lat=51.3)
    assert g.verdict(короткое, ОРСК, [длинная, другая])[0] == g.GEO_AMBIGUOUS
    # А полное название клиента с той же улицей — сходится, в любом падеже.
    полное = g.Parsed(street="ул 40 лет Октября", house="5")
    assert g.verdict(полное, ОРСК, [длинная])[0] == g.GEO_EXACT


def test_короткое_название_не_совпадает_по_основе() -> None:
    """«Ленина» и «Ленинградская» делят пять букв — но это разные улицы."""
    parsed = g.Parsed(street="ул Ленина", house="5")
    hit = дом(street="Ленинградская улица", house="5", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_STREET_MISMATCH


def test_два_дома_в_разных_посёлках_это_ambiguous() -> None:
    """«Ленина 5» — в двух посёлках округа. Без посёлка от клиента выбирать нельзя."""
    parsed = g.Parsed(street="ул Ленина", house="5")
    hits = [
        дом(street="улица Ленина", house="5", settlement="Ударник", lat=51.1, lon=58.4),
        дом(street="улица Ленина", house="5", settlement="Заречный", lat=51.3, lon=58.6),
    ]
    assert g.verdict(parsed, ОРСК, hits)[0] == g.GEO_AMBIGUOUS
    # Назвал посёлок — выбор однозначен.
    с_посёлком = g.Parsed(
        street="ул Ленина", house="5", settlement="Ударник", settlement_type="посёлок"
    )
    статус, hit = g.verdict(с_посёлком, ОРСК, hits)
    assert статус == g.GEO_EXACT and hit is not None and hit.settlement == "Ударник"


def test_посёлок_клиента_обязан_быть_в_ответе() -> None:
    parsed = g.Parsed(
        street="ул Ленина", house="5", settlement="Ударник", settlement_type="посёлок"
    )
    hit = дом(street="улица Ленина", house="5", settlement="Заречный")
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_SETTLEMENT_MISMATCH


def test_город_названный_клиентом_перебивает_город_объявления() -> None:
    """«г Гай, ул Ленина 5» в объявлении Орска: дом в Орске — не его дом."""
    parsed = g.Parsed(street="ул Ленина", house="5", locality="Гай")
    hit = дом(street="улица Ленина", house="5", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_OTHER_CITY


def test_дом_в_другом_городе_того_же_региона_не_подтверждается() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    hit = дом(street="улица Ленина", house="5", settlement=None, city="Новоорск")
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_CITY_MISMATCH


def test_дом_в_другой_области_не_подтверждается() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    hit = дом(street="улица Ленина", house="5", settlement=None, region="Орловская область")
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_REGION_MISMATCH


def test_регион_вместо_города_для_московского_слага() -> None:
    """Объявление «Москва и МО»: город не проверяется, регион — обязательно."""
    parsed = g.Parsed(street="ул Ленина", house="5")
    в_области = дом(
        street="улица Ленина",
        house="5",
        settlement=None,
        city="Люберцы",
        region="Московская область",
    )
    assert g.verdict(parsed, МОСКВА_МО, [в_области])[0] == g.GEO_EXACT
    в_туле = дом(
        street="улица Ленина", house="5", settlement=None, city="Тула", region="Тульская область"
    )
    assert g.verdict(parsed, МОСКВА_МО, [в_туле])[0] == g.GEO_REGION_MISMATCH
    assert g.build_query(parsed, МОСКВА_МО) == g.Query(
        region="Москва и область", city=None, settlement=None, street="ул Ленина", house="5"
    )


def test_улица_без_дома_и_интерполяция_это_не_дом() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    улица = дом(street="улица Ленина", house=None, house_level=False)
    assert g.verdict(parsed, ОРСК, [улица])[0] == g.GEO_HOUSE_MISSING
    догадка = дом(street="улица Ленина", house="5", interpolated=True)
    assert g.verdict(parsed, ОРСК, [догадка])[0] == g.GEO_HOUSE_MISSING


def test_нет_города_и_нет_ответов() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    assert g.verdict(parsed, None, [дом()])[0] == g.GEO_NO_CITY
    assert g.verdict(parsed, ОРСК, [])[0] == g.GEO_NOT_FOUND
    assert g.build_query(parsed, None) is None


# --- нормализация -------------------------------------------------------------


@pytest.mark.parametrize(
    ("а", "б"),
    [
        ("12 корпус 2", "12к2"),
        ("12 к 2", "12к2"),
        ("5 а", "5а"),
        ("2 В", "2в"),
        ("12 стр 1", "12с1"),
    ],
)
def test_ключ_дома(а: str, б: str) -> None:
    assert g.house_key(а) == g.house_key(б)


def test_ключ_дома_различает_соседей() -> None:
    assert g.house_key("12") != g.house_key("12к2")
    assert g.house_key("5") != g.house_key("5а")


@pytest.mark.parametrize(
    ("сокращённо", "полностью"),
    [
        ("ул звенигородская", "улица звенигородская"),
        ("пр-кт Ленина", "проспект Ленина"),
        ("наб. Фонтанки", "набережная Фонтанки"),
        ("12-я линия", "12-я линия"),
    ],
)
def test_тип_улицы_раскрывается_для_карты(сокращённо: str, полностью: str) -> None:
    assert g.expand_street(сокращённо) == полностью


@pytest.mark.parametrize(
    ("объявление", "карта"),
    [
        ("Оренбургская область", "Оренбургская область"),
        ("Москва и область", "Московская область"),
        ("Москва", "Москва"),
        ("Санкт-Петербург", "Ленинградская область"),
        ("ХМАО — Югра", "Ханты-Мансийский автономный округ — Югра"),
        ("Еврейская АО", "Еврейская автономная область"),
        ("Кабардино-Балкария", "Кабардино-Балкарская Республика"),
        ("Северная Осетия", "Республика Северная Осетия — Алания"),
        ("Республика Саха (Якутия)", "Республика Саха (Якутия)"),
    ],
)
def test_регион_объявления_сходится_с_написанием_карты(объявление: str, карта: str) -> None:
    assert g.region_matches(объявление, карта)


@pytest.mark.parametrize(
    ("объявление", "карта"),
    [
        ("Республика Алтай", "Алтайский край"),
        ("Краснодарский край", "Красноярский край"),
        ("Оренбургская область", "Орловская область"),
        ("Оренбургская область", None),
    ],
)
def test_похожие_регионы_не_сходятся(объявление: str, карта: str | None) -> None:
    assert not g.region_matches(объявление, карта)


def test_каждый_регион_справочника_сходится_сам_с_собой_и_ни_с_кем_чужим() -> None:
    from app.integrations.avito.listing_url import CITIES

    регионы = sorted({c.region for c in CITIES.values()})
    assert all(g.region_matches(r, r) for r in регионы)
    чужие = {(a, b) for a in регионы for b in регионы if a != b and g.region_matches(a, b)}
    # Единственные законные пары — город федерального значения и его область,
    # в обе стороны (ревью 14.09: из подмосковного объявления называют Москву).
    assert чужие <= {
        ("Москва", "Москва и область"),
        ("Москва", "Московская область"),
        ("Москва и область", "Москва"),
        ("Москва и область", "Московская область"),
        ("Московская область", "Москва"),
        ("Московская область", "Москва и область"),
        ("Санкт-Петербург", "Ленинградская область"),
        ("Ленинградская область", "Санкт-Петербург"),
        ("Севастополь", "Республика Крым"),
        ("Республика Крым", "Севастополь"),
    }


def test_запрос_собирается_из_компонентов_а_не_из_текста() -> None:
    """В запрос не попадает ничего, кроме улицы, дома, пункта и города."""
    q = g.build_query(ЗАРЕЧНЫЙ, ОРСК)
    assert q == g.Query(
        region="Оренбургская область",
        city="Орск",
        settlement="Заречный",
        street="ул звенигородская",
        house="1",
    )
    assert q.free_text == "Оренбургская область, Орск, Заречный, улица звенигородская 1"


# --- находки ревью 11.09 -----------------------------------------------------


@pytest.mark.parametrize(
    ("клиент", "карта"),
    [
        ("ул Советов", "Советская улица"),
        ("ул Пушкина", "Пушкинская улица"),
        ("ул Гагарина", "Гагаринская улица"),
        ("Октября", "Октябрьская улица"),
        ("Комсомольский пр", "Комсомольская улица"),
    ],
)
def test_однокоренные_названия_это_разные_улицы(клиент: str, карта: str) -> None:
    """Пять первых букв совпадают, а улицы разные (ревью 11.09)."""
    parsed = g.Parsed(street=клиент, house="5")
    hit = дом(street=карта, house="5", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_STREET_MISMATCH


def test_словоформы_одной_улицы_сходятся() -> None:
    parsed = g.Parsed(street="ул Первомайской", house="5")
    hit = дом(street="Первомайская улица", house="5", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_EXACT


def test_город_клиента_в_косвенном_падеже_это_свой_город() -> None:
    """«в городе Орске, ул Ленина 5» при объявлении из Орска — не другой город."""
    for лок in ("Орске", "Орска"):
        parsed = g.Parsed(street="ул Ленина", house="5", locality=лок)
        hit = дом(street="улица Ленина", house="5", settlement=None)
        assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_EXACT, лок
    parsed = g.Parsed(street="ул Ленина", house="5", locality="Гае")
    assert (
        g.verdict(parsed, ОРСК, [дом(street="улица Ленина", house="5", settlement=None)])[0]
        == g.GEO_OTHER_CITY
    )
    # Короткий город: основу не срезать, но падеж узнать надо («в Уфе» → Уфа).
    уфа = City("Уфа", "Республика Башкортостан", "Asia/Yekaterinburg")
    parsed = g.Parsed(street="ул Ленина", house="5", locality="Уфе")
    hit = дом(
        street="улица Ленина",
        house="5",
        settlement=None,
        city="Уфа",
        region="Республика Башкортостан",
    )
    assert g.verdict(parsed, уфа, [hit])[0] == g.GEO_EXACT


def test_петербургские_линии_сходятся_несмотря_на_район_в_имени_карты() -> None:
    """Разбор гасит «В.О.», карта его возвращает: однобуквенные слова и район не считаются."""
    parsed = g.Parsed(street="12-я линия", house="7")
    hit = дом(
        street="12-я линия В.О.",
        house="7",
        settlement=None,
        city="Санкт-Петербург",
        region="Санкт-Петербург",
    )
    спб = City("Санкт-Петербург", "Санкт-Петербург", "Europe/Moscow")
    assert g.verdict(parsed, спб, [hit])[0] == g.GEO_EXACT


# --- опечатки и сокращения (просьба владельца 11.09) -------------------------


@pytest.mark.parametrize(
    ("клиент", "карта"),
    [
        ("ул Звенигародская", "Звенигородская улица"),  # одна буква
        ("ул Звенигор", "Звенигородская улица"),  # сокращение
        ("Первомайская", "Первомайская улица"),
        ("ул Комсомольская", "Комсомольская улица"),
    ],
)
def test_опечатка_и_сокращение_в_улице_не_ломают_вердикт(клиент: str, карта: str) -> None:
    parsed = g.Parsed(street=клиент, house="1")
    hit = дом(street=карта, house="1", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_EXACT


@pytest.mark.parametrize(
    ("клиент", "карта"),
    [
        ("ул Мира", "улица Миру"),  # короткое слово — только целиком
        ("ул Ленин", "Ленинградская улица"),  # обрубок короче шести букв
        ("ул Советов", "Советская улица"),  # другая улица, а не опечатка
        ("ул Кирова", "Кировская улица"),
    ],
)
def test_допуск_на_опечатку_не_пускает_чужие_улицы(клиент: str, карта: str) -> None:
    parsed = g.Parsed(street=клиент, house="1")
    hit = дом(street=карта, house="1", settlement=None)
    assert g.verdict(parsed, ОРСК, [hit])[0] == g.GEO_STREET_MISMATCH


def test_стенд_1309_дом_не_в_фиас_с_точной_точкой_и_два_типа_у_карты() -> None:
    """DaData отдаёт «ул Маршала Иванова, 12» уровнем улицы (в ФИАС только
    «12 стр 1»), но с точкой самого дома — это дом; «ул Николаевский проспект»
    (два типа) годится клиентскому «николаевский проспект»; «пр-д» — проезд;
    «Презжая» — «Проезжая» без буквы; дома в одной точке — один адрес."""
    from leadchat_gateway.providers import dadata as gw_dadata

    from app.integrations.avito.listing_url import CITIES

    СПБ = {
        "city": "Санкт-Петербург",
        "street_with_type": "ул Маршала Иванова",
        "geo_lat": "59.85",
        "geo_lon": "30.15",
        "region_with_type": "г Санкт-Петербург",
    }
    ответ = {
        "suggestions": [
            {"value": "a", "data": {**СПБ, "fias_level": "7", "qc_geo": "0", "house": "12"}},
            {
                "value": "b",
                "data": {
                    **СПБ,
                    "fias_level": "8",
                    "qc_geo": "0",
                    "house": "12",
                    "block_type": "стр",
                    "block": "1",
                },
            },
            {
                "value": "c",
                "data": {
                    "fias_level": "7",
                    "qc_geo": "2",
                    "city": "Новочеркасск",
                    "street_with_type": "ул Атаманская",
                    "house": "18/64",
                    "geo_lat": "47.4",
                    "geo_lon": "40.1",
                    "region_with_type": "Ростовская обл",
                },
            },
        ]
    }
    hits = [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ответ)]
    assert [h.house_level for h in hits] == [True, True, False]
    spb = CITIES["sankt-peterburg"]
    статус, hit = g.verdict(g.Parsed(street="Ул Маршала Иванова", house="12"), spb, hits[:2])
    assert статус == g.GEO_EXACT and hit is not None and hit.house == "12"

    assert g._улица_не_шире("ул Николаевский проспект", "николаевский проспект")
    assert g._улица_не_шире("Александровский пр-д", "Александровский проезд")
    assert g._улица_не_шире("ул Проезжая", "Презжая")
    assert not g._улица_не_шире("ул Ленина", "пр Ленина")

    королёв = CITIES["korolev"]

    def дом(settlement, lat, lon):  # noqa: ANN001
        return g.GeoHit(
            street="ул Горького",
            house="12Б",
            settlement=settlement,
            city="Королёв",
            region="Московская обл",
            lat=lat,
            lon=lon,
            house_level=True,
        )

    один, тот_же = дом(None, 55.92, 37.85), дом("Первомайский", 55.9202, 37.8503)
    assert len(g.distinct_addresses([один, тот_же])) == 1
    assert (
        g.verdict(g.Parsed(street="ул. Горького", house="12 Б"), королёв, [один, тот_же])[0]
        == g.GEO_EXACT
    )
