"""Город из ссылки на объявление и местное время клиента (требования 3-4 от 11 августа).

ЗАЧЕМ ЭТОТ ФАЙЛ. Владелец: «непонятно, на какое объявление пишет клиент,
из-за этого я не знаю город» и «хочу, чтобы после установления города
показывалось время города, по которому пишет клиент» — чтобы диспетчер не
звонил в три ночи. Города Авито не отдаёт нигде: он есть только в СТРОКЕ
ссылки на объявление. Значит вся фича стоит на разборе чужой строки, и цена
ошибки — либо чужой город в отчёте, либо ночной звонок живому человеку.

ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ, ПО ПУНКТАМ (и как это ломать, чтобы убедиться, что
охрана работает — проверено, каждый пункт краснел):

1. `avito.ru.evil.com` отвергается. Замените сравнение hostname на
   `if "avito.ru" not in host` — падает `test_lookalike_domain_is_foreign`.
2. Город берётся ТОЛЬКО по справочнику. Добавьте обратную транслитерацию —
   падает `test_city_name_is_a_dictionary_not_a_transliteration`.
3. Часовые пояса настоящие. Замените `Europe/Kaliningrad` на `Europe/Moscow` —
   падает `test_every_timezone_exists_and_has_the_right_offset`.
4. Разбор не бросает НИКОГДА. Уберите `try` вокруг `urlsplit` — падает
   `test_never_raises_on_random_garbage`.
5. Город доезжает до API. Уберите `**city_fields(slug)` из `item_out` —
   падает `test_api_gives_city_and_timezone`.
6. Город появляется и у диалогов, записанных до выкатки. Уберите разбор
   ссылки из `item_out` — падает `test_city_is_derived_from_url_when_column_is_empty`.
7. Ссылки на профиль сегодня нет. Верните из `client_profile_url` строку —
   падает `test_client_profile_url_is_none_on_purpose`.

ДОБАВЛЕНО 12 АВГУСТА, ПО БОЕВЫМ ДАННЫМ ДВУХ КАНАЛОВ (раздел 2b ниже). Из 18
городов, из которых писали живые клиенты, три слага справочник не знал:
`odintsovo`, `moskva_zelenograd`, `leningradskaya_oblast_kommunar`. Отсюда ещё
пять пунктов охраны — и каждый проверен тем же способом, ломанием:

8. Три боевых слага в справочнике. Уберите `odintsovo` — падает
   `test_three_production_slugs_are_known`.
9. Приставка области доверяет только СВОЕМУ региону. Уберите сверку
   `city.region == region` — падает
   `test_region_prefix_of_another_region_is_refused`.
10. Хвост «город_район» — это район, а не область и не второй город. Уберите
    `_looks_like_district` — падают `test_region_suffix_is_not_a_district` и
    `test_second_city_is_not_a_district`.
11. Город из двух слов остаётся городом. Режьте слаг только по первому
    подчёркиванию — падает `test_two_word_city_keeps_its_district`.
12. Точное совпадение бьёт правила. Пустите разбор составного слага раньше
    `CITIES.get(slug)` — падает `test_compound_rules_do_not_touch_simple_slugs`.
13. Разбор дёшев на любой строке. Уберите потолок `_MAX_SLUG_WORDS` — падает
    `test_compound_parsing_stays_cheap_on_a_long_slug`.
14. Оборванный слаг не город. Уберите проверку пустого хвоста в
    `_looks_like_district` — падает `test_trailing_underscore_is_not_a_district`.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from app.integrations.avito import listing_url
from app.integrations.avito.listing_url import (
    _REGION_PREFIX_ALIASES,
    _REGION_PREFIXES,
    CITIES,
    City,
    city_by_slug,
    city_fields,
    parse_listing_url,
)
from app.models import Client, Conversation

# --------------------------------------------------------------------------
# 1. Разбор ссылки (docs/32 §3)
# --------------------------------------------------------------------------

CANON = "https://www.avito.ru/kerch/predlozheniya_uslug/remont_televizorov_8213779975"


def test_canonical_link_gives_id_city_and_category() -> None:
    """Канонический пример из задания: номер, город, категория."""
    p = parse_listing_url(CANON)
    assert p.kind == "item"
    assert p.ok is True
    assert p.item_id == 8213779975
    assert (p.city_slug, p.city_name) == ("kerch", "Керчь")
    assert p.city_region == "Республика Крым"
    assert p.city_tz == "Europe/Simferopol"
    assert p.category_slug == "predlozheniya_uslug"
    assert p.title_slug == "remont_televizorov"


def test_mobile_host_is_the_same_avito() -> None:
    """m.avito.ru — та же ссылка, присланная с телефона."""
    p = parse_listing_url("https://m.avito.ru/moskva/bytovaya_tehnika/holodilnik_1234567")
    assert (p.kind, p.city_name) == ("item", "Москва")


def test_plain_http_is_accepted() -> None:
    """http:// без TLS встречается в старых пересылках."""
    assert parse_listing_url("http://avito.ru/kerch/x_8213779975").city_name == "Керчь"


def test_no_scheme_at_all() -> None:
    """«avito.ru/...» без схемы: иначе весь адрес был бы разобран как путь."""
    assert parse_listing_url("avito.ru/kazan/x_8213779975").city_name == "Казань"


def test_query_is_dropped_entirely() -> None:
    """В query ходят рекламные метки и base64-контексты — доверять нечему."""
    p = parse_listing_url(CANON + "?utm_source=mail&context=H4sIAAAA_8888888888")
    assert p.item_id == 8213779975  # не 8888888888 из метки


def test_fragment_is_dropped_entirely() -> None:
    p = parse_listing_url(CANON + "#comments")
    assert p.item_id == 8213779975


def test_short_link_has_id_but_no_city() -> None:
    """avito.ru/3060161080 — короткая ссылка: номер есть, города нет."""
    p = parse_listing_url("https://avito.ru/3060161080")
    assert (p.kind, p.item_id, p.city_slug) == ("short", 3060161080, None)


def test_items_route_is_not_a_city() -> None:
    """«items» — служебный сегмент, а не город Итемс."""
    p = parse_listing_url("https://www.avito.ru/items/3060161080")
    assert (p.kind, p.item_id, p.city_slug) == ("item", 3060161080, None)


def test_brand_page_keeps_the_id() -> None:
    p = parse_listing_url("https://www.avito.ru/brands/xxx/remont_8213779975")
    assert (p.kind, p.item_id) == ("brand", 8213779975)


def test_user_page_must_not_look_like_a_listing() -> None:
    """Профиль — не объявление. Номер обязан обнулиться, иначе хеш профиля
    уехал бы в базу как item_id и склеил бы разные объявления."""
    p = parse_listing_url("https://www.avito.ru/user/a7f3c1d29b4e5678901234/profile")
    assert p.kind == "user"
    assert p.item_id is None
    assert p.ok is False


def test_digits_inside_the_slug_are_not_the_id() -> None:
    """«iPhone 13» в названии не должен становиться номером объявления."""
    p = parse_listing_url("https://avito.ru/moskva/telefony/remont_iphone_13_pro_max_8213779975")
    assert p.item_id == 8213779975


def test_unknown_city_slug_is_kept_but_not_named() -> None:
    """Незнакомый город: слаг сохраняем, название не выдумываем."""
    p = parse_listing_url("https://avito.ru/zazerkalye/uslugi/remont_8213779975")
    assert p.city_slug == "zazerkalye"
    assert p.city_name is None
    assert p.city_tz is None  # времени без пояса не показываем


def test_city_name_is_a_dictionary_not_a_transliteration() -> None:
    """ГЛАВНАЯ ЗАЩИТА СПРАВОЧНИКА.

    Обратная транслитерация дала бы «Ростов На Дону» и «Москва И Мо» — мусор
    в отчёте владельца. Названия берутся только из таблицы.
    """
    assert parse_listing_url("https://avito.ru/rostov-na-donu/x_1234567").city_name == (
        "Ростов-на-Дону"
    )
    assert parse_listing_url("https://avito.ru/moskva_i_mo/x_1234567").city_name == "Москва и МО"
    assert parse_listing_url("https://avito.ru/nizhniy_novgorod/x_1234567").city_name == (
        "Нижний Новгород"
    )


def test_lookalike_domain_is_foreign() -> None:
    """`avito.ru.evil.com` СОДЕРЖИТ «avito.ru» и обязан быть отвергнут.

    Проверка идёт по разобранному hostname, а не поиском подстроки. Ссылка с
    чужого домена не должна ни дать город, ни доехать до интерфейса кнопкой
    «Открыть на Авито».
    """
    p = parse_listing_url("https://avito.ru.evil.com/kerch/x_8213779975")
    assert p.kind == "foreign"
    assert (p.item_id, p.city_slug, p.ok) == (None, None, False)


def test_foreign_site_is_foreign() -> None:
    p = parse_listing_url("https://ozon.ru/moskva/product_8213779975")
    assert p.kind == "foreign"
    assert p.city_slug is None


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_input_is_not_an_error(raw: str | None) -> None:
    p = parse_listing_url(raw)
    assert (p.ok, p.kind) == (False, "garbage")


def test_absurdly_long_string_is_refused_without_work() -> None:
    p = parse_listing_url("https://avito.ru/" + "a" * 5000 + "_8213779975")
    assert p.ok is False


@pytest.mark.parametrize("raw", ["https://", "avito.ru", "://///", "http://", "//", "https://:80"])
def test_broken_urls_do_not_raise(raw: str) -> None:
    assert parse_listing_url(raw).ok is False


def test_percent_encoded_cyrillic_path() -> None:
    """Кириллица в пути приходит закодированной — разбираем после unquote."""
    p = parse_listing_url(
        "https://avito.ru/kerch/uslugi/%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82_8213779975"
    )
    assert p.item_id == 8213779975
    assert p.title_slug == "ремонт"


def test_encoded_slash_does_not_invent_a_segment() -> None:
    """%2F внутри имени — это символ имени, а не разделитель пути.

    Разбирай мы весь путь одним unquote (как в псевдокоде docs/32 §3),
    сегменты разъехались бы и категория уехала бы на место города.
    """
    p = parse_listing_url("https://avito.ru/kerch/uslugi%2Fremont/tv_8213779975")
    assert p.city_slug == "kerch"
    assert p.category_slug == "uslugi/remont"


def test_number_too_big_for_bigint_is_not_an_id() -> None:
    """23 цифры в хвосте — это не номер, а мусор; в базу такое не влезет."""
    p = parse_listing_url("https://avito.ru/kerch/uslugi/remont_99999999999999999999999")
    assert p.item_id is None
    assert p.kind == "garbage"


def test_never_raises_on_random_garbage() -> None:
    """500 случайных строк — функция не бросает НИ РАЗУ.

    Разбор ссылки живёт в конвейере входящих сообщений. Исключение здесь
    означает потерянное сообщение живого клиента, а на входе — строка из
    чужого API, которую никто нам не гарантировал.
    """
    rnd = random.Random(20260811)  # фиксированное зерно: падение воспроизводимо
    alphabet = "abcяё/:?#%.@[]\\ 0123456789_-\t\n™"
    for _ in range(500):
        raw = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
        parse_listing_url(raw)  # достаточно того, что не бросило
        parse_listing_url("https://avito.ru/" + raw)


# --------------------------------------------------------------------------
# 2. Справочник городов и часовых поясов
# --------------------------------------------------------------------------


def test_every_timezone_exists_and_has_the_right_offset() -> None:
    """Каждое имя пояса живёт в zoneinfo, и смещение — российское (+2…+12).

    Опечатка в имени («Europe/Moskow») не видна глазами и на боевом обернулась
    бы отсутствием времени у целого региона; неверный пояс — ночным звонком.
    """
    try:
        ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:  # pragma: no cover — среда без базы поясов
        pytest.skip("в системе нет базы часовых поясов (tzdata)")

    winter = datetime(2026, 1, 15, 12, 0)
    summer = datetime(2026, 7, 15, 12, 0)
    for slug, city in CITIES.items():
        tz = ZoneInfo(city.tz)
        w = tz.utcoffset(winter)
        s = tz.utcoffset(summer)
        assert w is not None and s is not None
        # В России нет перехода на летнее время с 2014 года: смещение обязано
        # совпадать зимой и летом. Расхождение значит, что мы приписали
        # российскому городу европейский пояс.
        assert w == s, f"{slug}: пояс {city.tz} переводит часы"
        hours = w.total_seconds() / 3600
        assert 2 <= hours <= 12, f"{slug}: {city.tz} даёт UTC+{hours}"


def test_all_eleven_russian_zones_are_covered() -> None:
    """В России 11 часовых зон. Пропущенная = город без времени."""
    try:
        ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:  # pragma: no cover
        pytest.skip("в системе нет базы часовых поясов (tzdata)")
    when = datetime(2026, 8, 11, 12, 0)
    offsets = set()
    for city in CITIES.values():
        delta = ZoneInfo(city.tz).utcoffset(when)
        assert delta is not None
        offsets.add(int(delta.total_seconds() // 3600))
    assert offsets == set(range(2, 13))


def test_millionniki_are_all_in_the_dictionary() -> None:
    """Города-миллионники — обязательный минимум покрытия."""
    million = [
        "moskva",
        "sankt-peterburg",
        "novosibirsk",
        "ekaterinburg",
        "kazan",
        "nizhniy_novgorod",
        "chelyabinsk",
        "samara",
        "omsk",
        "rostov-na-donu",
        "ufa",
        "krasnoyarsk",
        "voronezh",
        "perm",
        "volgograd",
        "krasnodar",
    ]
    assert [s for s in million if s not in CITIES] == []


def test_dictionary_slugs_look_like_avito_slugs() -> None:
    """Слаг — латиница, цифры, дефис и подчёркивание. Кириллица здесь означала
    бы, что кто-то вписал город руками и он никогда не совпадёт со ссылкой."""
    bad = [s for s in CITIES if not all(c.isascii() and (c.isalnum() or c in "-_") for c in s)]
    assert bad == []


def test_city_fields_shape_is_the_same_everywhere() -> None:
    """Одна форма города на API и на WS-патч — иначе фронт получал бы разный
    набор ключей при загрузке страницы и при живом обновлении."""
    assert city_fields("kerch") == {
        "city_slug": "kerch",
        "city_name": "Керчь",
        "city_region": "Республика Крым",
        "city_tz": "Europe/Simferopol",
    }
    assert city_fields("zazerkalye") == {
        "city_slug": "zazerkalye",
        "city_name": None,
        "city_region": None,
        "city_tz": None,
    }
    assert city_fields(None)["city_slug"] is None


# --------------------------------------------------------------------------
# 2b. Составные слаги и три города с боевого (12 августа)
# --------------------------------------------------------------------------
#
# Всё в этом разделе стоит на одном факте: на двух боевых каналах клиенты
# писали из 18 городов, и три слага справочник не опознал вовсе. Ни один из
# трёх не был выдуман — все три взяты из живых `item_url`.
#
# ЦЕНА РАЗБОРА СТЕРЕЖЁТСЯ СЧЁТОМ РАЗРЕЗОВ, А НЕ СЕКУНДОМЕРОМ (29.08). Здесь
# стоял замер стенных часов: 200 разборов слага-монстра обязаны были уложиться
# в секунду. На общей машине такой порог меряет не код, а соседей по прогону, и
# краснеет «иногда» — такой красный перестают читать. Свойство, ради которого
# порог ставили, считается точно: у разбора есть потолок на число слов, значит
# и число разрезов ограничено — сколько их перебрано, столько и стоит разбор
# (тот же ход, что у INT-1 в `tests/integration/test_inbound_pipeline_pg.py`).
#
# Стенные часы остаются там, где им и место: p95 разбора меряет нагрузочный
# прогон k6 (`tests/load/release.js`), потому что он задаёт условия замера.

#: Потолок разрезов на ОДИН составной слаг. В `listing_url` стоит
#: `_MAX_SLUG_WORDS = 8` (три слова самой длинной приставки
#: `respublika_saha_yakutiya` плюс город из двух — с запасом), а разрезов у
#: слага из восьми слов ровно семь. Больше семи означает, что потолок снят или
#: обойдён и длина чужой строки снова превращается в работу.
_COMPOUND_CUT_BUDGET = 7


class CutTally:
    """Сколько разрезов составного слага перебрал разбор.

    Считает обращения, а не время: счёт разрезов даёт один и тот же ответ на
    любой машине и при любом соседе по прогону.
    """

    def __init__(self) -> None:
        self.heads: list[str] = []

    def __len__(self) -> int:
        return len(self.heads)

    def reset(self) -> None:
        self.heads.clear()

    def report(self) -> str:
        """Первые головы одной строкой — чтобы падение сразу называло виновника."""
        shown = [f"  {i + 1}. {h[:60]}" for i, h in enumerate(self.heads[:5])]
        if len(self.heads) > 5:
            shown.append(f"  … и ещё {len(self.heads) - 5}")
        return "\n".join(shown)


@pytest.fixture
def cut_tally(monkeypatch: pytest.MonkeyPatch) -> CutTally:
    """Счётчик разрезов — на том шве, через который разбор ищет регион.

    `region_by_prefix` зовётся РОВНО РАЗ на каждый разрез составного слага и
    больше нигде: счёт её вызовов и есть число разрезов.
    """
    tally = CutTally()
    original = listing_url.region_by_prefix

    def _record(head: str) -> str | None:
        tally.heads.append(head)
        return original(head)

    monkeypatch.setattr(listing_url, "region_by_prefix", _record)
    return tally


def test_three_production_slugs_are_known() -> None:
    """Три слага, на которых справочник промахивался на живых диалогах.

    `odintsovo` (3 диалога) — тот же город, что лежавший в таблице `odincovo`,
    но в написании, которое Авито действительно присылает. `moskva_zelenograd`
    — форма «город_район», `leningradskaya_oblast_kommunar` — «область_город».
    Часовой пояс у всех трёх московский, и это проверено по фактам: Одинцово —
    Московская область, Зеленоград — административный округ самой Москвы,
    Коммунар — город Гатчинского района Ленинградской области (UTC+3).
    """
    assert city_fields("odintsovo") == {
        "city_slug": "odintsovo",
        "city_name": "Одинцово",
        "city_region": "Московская область",
        "city_tz": "Europe/Moscow",
    }
    assert city_fields("moskva_zelenograd") == {
        "city_slug": "moskva_zelenograd",
        "city_name": "Зеленоград",
        # Зеленоград — не область, а округ Москвы; регион обязан это отражать.
        "city_region": "Москва",
        "city_tz": "Europe/Moscow",
    }
    assert city_fields("leningradskaya_oblast_kommunar") == {
        "city_slug": "leningradskaya_oblast_kommunar",
        "city_name": "Коммунар",
        "city_region": "Ленинградская область",
        "city_tz": "Europe/Moscow",
    }


def test_production_slug_survives_a_whole_link() -> None:
    """Составной слаг доезжает не только до справочника, но и через разбор ссылки.

    Проверять `city_by_slug` в одиночку мало: город в интерфейс попадает через
    `parse_listing_url`, и разъедься эти два пути — на экране был бы прочерк
    при зелёном тесте справочника.
    """
    p = parse_listing_url(
        "https://www.avito.ru/leningradskaya_oblast_kommunar"
        "/predlozheniya_uslug/remont_holodilnikov_8213779975"
    )
    assert p.kind == "item"
    assert p.city_slug == "leningradskaya_oblast_kommunar"
    assert (p.city_name, p.city_tz) == ("Коммунар", "Europe/Moscow")
    assert p.category_slug == "predlozheniya_uslug"


def test_ts_spelling_of_c_cities_resolves_to_the_same_city() -> None:
    """«ц» через «ts» — то же самое место, что «ц» через «c».

    Боевой `odintsovo` доказал, что написание через «c», по которому собрана
    таблица, — догадка. Пара написаний обязана давать ОДИН и тот же город:
    разойдись они хоть регионом, в отчёте по городам появилось бы два Липецка.
    """
    pairs = [
        ("odincovo", "odintsovo"),
        ("lyubercy", "lyubertsy"),
        ("cherepovec", "cherepovets"),
        ("lipeck", "lipetsk"),
        ("elec", "elets"),
        ("rubcovsk", "rubtsovsk"),
        ("novokuzneck", "novokuznetsk"),
        ("leninsk-kuzneckiy", "leninsk-kuznetskiy"),
        ("novotroick", "novotroitsk"),
    ]
    for with_c, with_ts in pairs:
        assert CITIES[with_c] == CITIES[with_ts], f"{with_c} и {with_ts} разъехались"


def test_region_prefix_plus_known_city() -> None:
    """«область_город»: приставка области + город, который мы знаем."""
    assert city_by_slug("moskovskaya_oblast_himki") == CITIES["himki"]
    assert city_by_slug("leningradskaya_oblast_vyborg") == City(
        "Выборг", "Ленинградская область", "Europe/Moscow"
    )
    assert city_by_slug("krasnodarskiy_kray_sochi") == CITIES["sochi"]
    # Приставка сама из трёх слов — голова обязана примеряться целиком.
    assert city_by_slug("respublika_saha_yakutiya_neryungri") == CITIES["neryungri"]
    # А эта приставка КОНЧАЕТСЯ словом-признаком региона («ao»), которым правило
    # «город_район» бракует хвосты. Приставку это бракование задевать не должно.
    assert city_by_slug("hanty-mansiyskiy_ao_surgut") == CITIES["surgut"]


def test_region_prefix_of_another_region_is_refused() -> None:
    """ЛОЖНОЕ СРАБАТЫВАНИЕ, КОТОРОЕ СТОИЛО БЫ НОЧНОГО ЗВОНКА.

    Железногорсков в России два: курский (UTC+3) и красноярский (UTC+7). В
    справочнике лежит красноярский. Разбери мы `kurskaya_oblast_zheleznogorsk`
    «по хвосту», диспетчер увидел бы у курского клиента красноярское время —
    промах на четыре часа, ровно та беда, ради которой в этом модуле вообще
    запрещено угадывать. Спасает сверка региона приставки с регионом города.
    """
    # ⚠ ОТВЕТ ИЗМЕНИЛСЯ 28.08, А ПРАВИЛО — НЕТ. Раньше здесь было `is None`:
    # справочник курского Железногорска не знал, и УГАДЫВАТЬ его по хвосту
    # запрещено — промах на четыре часа. Теперь город назван точно (замер боя дал
    # 51 диалог из этого города), и точное совпадение бьёт любые правила разбора:
    # это не догадка, а запись в справочнике.
    #
    # Смысл проверки от этого не пострадал, и вот он: пояс обязан быть КУРСКИЙ.
    курский = city_by_slug("kurskaya_oblast_zheleznogorsk")
    assert курский is not None and курский.tz == "Europe/Moscow", (
        "курский Железногорск определился не своим поясом — ровно та беда, "
        "ради которой в этом модуле запрещено угадывать"
    )
    assert курский.region == "Курская область"
    # А своя область разбирается: правило не «запрещаем всё», а «сверяем».
    # Ключ теперь сам составной: голого «zheleznogorsk» в справочнике нет —
    # с двумя Железногорсками он был лотереей на четыре часа, а в бою такая
    # форма не приходит ни разу (замер 28.08).
    красноярский = city_by_slug("krasnoyarskiy_kray_zheleznogorsk")
    assert красноярский == CITIES["krasnoyarskiy_kray_zheleznogorsk"]
    assert красноярский is not None and красноярский.tz == "Asia/Krasnoyarsk"
    # А голый слаг честно молчит: гадать между четырьмя часами разницы нельзя.
    assert city_by_slug("zheleznogorsk") is None


def test_region_prefix_with_unknown_city_invents_nothing() -> None:
    """Приставка известна, город — нет. Имя города не выдумывается из слага."""
    assert city_by_slug("moskovskaya_oblast_zazerkalye") is None
    assert city_fields("moskovskaya_oblast_zazerkalye") == {
        "city_slug": "moskovskaya_oblast_zazerkalye",
        "city_name": None,
        "city_region": None,
        "city_tz": None,
    }


def test_city_with_a_district_falls_back_to_the_city() -> None:
    """«город_район»: имени района мы не знаем, а пояс у него городской.

    Показываем родительский город. Он и по существу верен: Бутово — это
    Москва, Колпино — Санкт-Петербург, Адлер — Сочи.
    """
    assert city_by_slug("moskva_butovo") == CITIES["moskva"]
    assert city_by_slug("sankt-peterburg_kolpino") == CITIES["sankt-peterburg"]
    assert city_by_slug("sochi_adler") == CITIES["sochi"]


def test_two_word_city_keeps_its_district() -> None:
    """Город из двух слов не должен разваливаться на первом подчёркивании.

    Режь мы слаг только по первому `_`, головой у `nizhniy_novgorod_...` стало
    бы «nizhniy» — не город ничей, и Нижний Новгород пропал бы вместе с
    районом. Поэтому голова примеряется от самой длинной к самой короткой.
    """
    assert city_by_slug("nizhniy_novgorod_avtozavodskiy") == CITIES["nizhniy_novgorod"]
    assert city_by_slug("sergiev_posad_severnyy") == CITIES["sergiev_posad"]


def test_region_suffix_is_not_a_district() -> None:
    """ЛОЖНОЕ СРАБАТЫВАНИЕ: «город_область» — уточнение, а не район.

    `zheleznogorsk_kurskaya_oblast` — это КУРСКИЙ Железногорск, а в справочнике
    под голым `zheleznogorsk` лежит красноярский. Прими мы хвост за район,
    получили бы UTC+7 у клиента из UTC+3.
    """
    assert city_by_slug("zheleznogorsk_kurskaya_oblast") is None
    # То же самое для области, которой в таблице приставок ещё нет: ловим по
    # слову-признаку, а не по перечислению.
    assert city_by_slug("zheleznogorsk_zazerkalskaya_oblast") is None
    assert city_by_slug("kirov_kaluzhskaya_oblast") is None


def test_second_city_is_not_a_district() -> None:
    """ЛОЖНОЕ СРАБАТЫВАНИЕ: два города подряд — не город с районом.

    Который из двух клиентский, по слагу не видно, поэтому честный ответ —
    «не знаю», а не первый попавшийся.
    """
    assert city_by_slug("moskva_kazan") is None
    assert city_by_slug("sochi_anapa") is None


def test_compound_rules_do_not_touch_simple_slugs() -> None:
    """ГЛАВНАЯ ОХРАНА СОВМЕСТИМОСТИ: точное совпадение бьёт любые правила.

    В справочнике полно слагов с подчёркиванием, и разбор не имеет права их
    трогать. Самый опасный — `moskva_i_mo`: по правилу «город_район» он
    превратился бы в «Москву», и агрегат Авито «Москва и МО» молча потерял бы
    область в отчёте владельца.
    """
    expected = {
        "moskva_i_mo": ("Москва и МО", "Москва и область"),
        "nizhniy_novgorod": ("Нижний Новгород", "Нижегородская область"),
        "velikiy_novgorod": ("Великий Новгород", "Новгородская область"),
        "naberezhnye_chelny": ("Набережные Челны", "Республика Татарстан"),
        "sergiev_posad": ("Сергиев Посад", "Московская область"),
        "staryy_oskol": ("Старый Оскол", "Белгородская область"),
        "novyy_urengoy": ("Новый Уренгой", "Ямало-Ненецкий АО"),
        "velikie_luki": ("Великие Луки", "Псковская область"),
        "moskva_zelenograd": ("Зеленоград", "Москва"),
        "leningradskaya_oblast_kommunar": ("Коммунар", "Ленинградская область"),
    }
    for slug, (name, region) in expected.items():
        city = city_by_slug(slug)
        assert city is not None, slug
        assert (city.name, city.region) == (name, region), slug


def test_unknown_compound_slug_stays_unknown() -> None:
    """Незнакомый составной слаг ведёт себя как прежде: слаг есть, времени нет.

    Это поведение менять было нельзя — оно и не изменилось.
    """
    p = parse_listing_url("https://avito.ru/zazerkalye_krivogo_zerkala/uslugi/remont_8213779975")
    assert p.city_slug == "zazerkalye_krivogo_zerkala"
    assert (p.city_name, p.city_region, p.city_tz) == (None, None, None)


def test_compound_parsing_stays_cheap_on_a_long_slug(cut_tally: CutTally) -> None:
    """Слаг из сотен подчёркиваний не должен стоить перебора всех разрезов.

    Разбор ссылки зовётся полсотни раз на одну выдачу списка диалогов, а первым
    сегментом пути приходит что угодно: это чужая строка. Без потолка на число
    слов каждый такой слаг давал бы квадрат по длине — на ровном месте.

    Стережём сам потолок: сколько разрезов разбор перебрал и растёт ли это число
    вместе с длиной слага. Ответ один на любой машине — в отличие от секундомера,
    который здесь стоял раньше.
    """
    # СНАЧАЛА ПРОВЕРЯЕМ СЧЁТЧИК, А НЕ КОД. Разъедься шов — и ноль разрезов начал бы
    # означать «счётчик ослеп», а тест зеленел бы на любом переборе.
    assert city_by_slug("nizhniy_novgorod_avtozavodskiy") == CITIES["nizhniy_novgorod"]
    assert len(cut_tally) >= 1, "счётчик не видит разбора составного слага — шов переехал"

    counts: list[int] = []
    for words in (500, 1000):
        cut_tally.reset()
        monster = "_".join(["a"] * words)
        assert city_by_slug(monster) is None
        counts.append(len(cut_tally))
        assert len(cut_tally) <= _COMPOUND_CUT_BUDGET, (
            f"слаг из {words} слов стоил {len(cut_tally)} разрезов при бюджете "
            f"{_COMPOUND_CUT_BUDGET}:\n{cut_tally.report()}"
        )
    # Длина строки — чужая, и цена разбора не имеет права от неё зависеть.
    assert counts[0] == counts[1], (
        f"цена разбора выросла с длиной слага: {counts[0]} разрезов на 500 слов "
        f"против {counts[1]} на 1000"
    )


def test_trailing_underscore_is_not_a_district() -> None:
    """«moskva_» — оборванный слаг, а не Москва с районом.

    Пустой хвост попадал в правило «город_район» и молча давал Москву.
    """
    assert city_by_slug("moskva_") is None


def test_region_prefixes_point_at_regions_that_exist() -> None:
    """Каждая приставка обязана указывать на регион, который есть в справочнике.

    Опечатка в русской половине таблицы («Московская обл.») не видна глазами и
    выключила бы разбор целой области молча: сверка региона просто перестала бы
    совпадать, и все составные слаги этой области стали бы неизвестными.
    """
    regions = {c.region for c in CITIES.values()}
    orphans = sorted({r for r in _REGION_PREFIXES.values() if r not in regions})
    assert orphans == []
    used = list(_REGION_PREFIXES.values())
    duplicates = sorted({r for r in used if used.count(r) > 1})
    assert duplicates == [], "две приставки на один регион — разбор станет зависеть от порядка"


def test_region_prefixes_look_like_avito_slugs() -> None:
    """Приставка — латиница в нижнем регистре: иначе она не совпадёт никогда."""
    bad = [
        p
        for p in _REGION_PREFIXES
        if p != p.lower() or not all(c.isascii() and (c.isalnum() or c in "-_") for c in p)
    ]
    assert bad == []


def test_one_region_never_has_two_timezones() -> None:
    """Согласованность справочника: регион не может жить в двух поясах.

    Пропущенная строка при добавлении города («вписал Челябинскую область, а
    пояс скопировал московский») именно так и выглядит — и даёт ночной звонок
    ровно одному городу, которого никто не проверит.
    """
    zones: dict[str, set[str]] = {}
    for city in CITIES.values():
        zones.setdefault(city.region, set()).add(city.tz)
    split = {r: tz for r, tz in zones.items() if len(tz) > 1}
    assert split == {}


def test_same_city_name_never_means_two_different_places() -> None:
    """Одноимённые города разрешены ТОЛЬКО под уточняющими слагами.

    Железногорск, Кировск, Октябрьский — таких пар в России десятки, и разница
    между ними бывает в четыре часа. Вписать второй одноимённый город голым
    слагом значит сделать разбор лотереей; вписывать его надо формой
    «область_город», где регион виден.
    """
    places: dict[str, set[tuple[str, str]]] = {}
    slugs: dict[str, list[str]] = {}
    for slug, city in CITIES.items():
        places.setdefault(city.name, set()).add((city.region, city.tz))
        slugs.setdefault(city.name, []).append(slug)
    for name, variants in places.items():
        if len(variants) == 1:
            continue
        for slug in slugs[name]:
            head = slug.rsplit("_", 1)[0] if "_" in slug else ""
            # Короткие формы площадки признаются наравне: см.
            # `_REGION_PREFIX_ALIASES` — правило «одна приставка на регион» там
            # не нарушено, у региона просто два написания. Замер боя 28.08 дал
            # `bashkortostan_oktyabrskiy` без «respublika_».
            assert head in _REGION_PREFIXES or head in _REGION_PREFIX_ALIASES, (
                f"{name} встречается в разных регионах, а слаг {slug} этого не показывает"
            )


# --------------------------------------------------------------------------
# 3. Задача обогащения: город из ссылки, без единого запроса в Авито
# --------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, channel: str, payload: Any) -> int:
        self.published.append((channel, payload))
        return 0


class _LogSpy:
    """Подмена structlog-логгера: запоминает событие и его поля."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    info = _record
    warning = _record
    debug = _record

    def events(self) -> list[str]:
        return [e for e, _ in self.calls]

    def fields_of(self, event: str) -> dict[str, Any]:
        return next(kw for e, kw in self.calls if e == event)


async def _seed_conv(db_sessionmaker: Any, account: Any, **conv_kw: Any) -> uuid.UUID:
    async with db_sessionmaker() as db:
        client_row = Client(
            id=uuid.uuid4(),
            channel="avito",
            external_id="923456789",
            name="Иван",
            # Фото выдано заранее: с 15.08 обогащение добирает и его, и пустой
            # аватар делал бы поход в Авито ЗАКОННЫМ — а эти случаи проверяют
            # ровно то, что похода нет.
            avatar_url="https://static.avito.ru/i/ivan.png",
            # По той же причине отмечен и профиль: с 02.09 обогащение спрашивает
            # Авито про ссылку на профиль, и клиент, которого ещё не спрашивали,
            # делал бы поход законным. А проверяется здесь ровно обратное — что
            # города берутся из ссылки объявления, никуда не ходя.
            profile_checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        db.add(client_row)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-city",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
            **conv_kw,
        )
        db.add(conv)
        await db.commit()
        return conv.id


async def test_city_is_filled_from_a_known_url_without_touching_avito(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Объявление уже известно, имя тоже — но города нет.

    Ровно такие диалоги накопились до выкатки и приезжают, когда объявление
    пришло прямо в вебхуке. Раньше задача выходила по «всё уже известно» и
    город не появлялся бы никогда — это дефект 2 из docs/32, повторённый
    заново. Похода в Авито при этом не делаем: город лежит в ссылке.
    """
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666777)
    conv_id = await _seed_conv(
        db_sessionmaker,
        account,
        item_title="Ремонт телевизоров",
        item_url=CANON,
    )

    async def _no_network(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("за карточкой ходить незачем: город берётся из ссылки")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _no_network)
    redis = _FakeRedis()
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": redis}, conv_id
    )

    async with db_sessionmaker() as db:
        fresh = await db.get(Conversation, conv_id)
        assert fresh is not None
        assert fresh.item_city_slug == "kerch"
    assert redis.published, "город должен уехать в интерфейс живым обновлением"


async def test_city_snapshot_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Объявление переехало в другой город — снимок обращения не меняется.

    Иначе срез «обращения по городам» за прошлый квартал переписался бы задним
    числом, причём молча.
    """
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666778)
    conv_id = await _seed_conv(
        db_sessionmaker,
        account,
        item_title="Ремонт телевизоров",
        item_url="https://avito.ru/moskva/uslugi/remont_8213779975",
        item_city_slug="kerch",  # обращение пришло, когда объявление было в Керчи
    )

    async def _no_network(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("ходить незачем")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _no_network)
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}, conv_id
    )

    async with db_sessionmaker() as db:
        fresh = await db.get(Conversation, conv_id)
        assert fresh is not None
        assert fresh.item_city_slug == "kerch"


async def test_unknown_city_slug_is_logged_once_at_write_time(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Незнакомый слаг попадает в лог — справочник растёт по факту.

    Лог пишется ЗДЕСЬ, а не в самом разборе: разбор зовётся и на выдаче
    списка, и лог из него утонул бы в повторах на каждое открытие экрана.
    """
    from app.services import client_enrich

    spy = _LogSpy()
    monkeypatch.setattr(client_enrich, "log", spy)
    account = await make_avito_account(avito_user_id=555666779)
    conv_id = await _seed_conv(
        db_sessionmaker,
        account,
        item_title="Ремонт",
        item_url="https://avito.ru/zazerkalye/uslugi/remont_8213779975",
    )

    async def _no_network(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("ходить незачем")

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _no_network)
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": _FakeRedis()}, conv_id
    )

    assert "listing.unknown_city_slug" in spy.events()
    assert spy.fields_of("listing.unknown_city_slug")["slug"] == "zazerkalye"


async def test_enrichment_sends_the_item_nested_not_flat(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Объявление в WS-патче — ВЛОЖЕННЫМ `item`, как объявлен ConversationPatch.

    Плоские `item_title/item_url/item_price` фронт кладёт в корень строки, а
    `item` оставляет пустым: карточка объявления не появлялась до перезагрузки
    страницы. Имя клиента при этом приезжало правильно — оно и было вложенным.
    """
    import json

    from app.integrations.avito.adapter import ChatInfo
    from app.services import client_enrich

    account = await make_avito_account(avito_user_id=555666780)
    conv_id = await _seed_conv(db_sessionmaker, account)

    async def _fake_fetch(*_a: Any, **_kw: Any) -> ChatInfo:
        return ChatInfo(
            external_chat_id="chat-city",
            client_external_id="923456789",
            client_name="Иван",
            item_title="Ремонт телевизоров",
            item_url=CANON,
            item_price="от 1 500 ₽",
            unread_count=0,
            has_unread=False,
            last_message_at=None,
        )

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    redis = _FakeRedis()
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": redis}, conv_id
    )

    assert redis.published, "обновление обязано уехать в интерфейс"
    _, raw = redis.published[-1]
    frame = json.loads(raw) if isinstance(raw, str | bytes) else raw
    patch = frame["data"]["patch"]
    assert "item_title" not in patch, "плоский ключ фронт положит мимо карточки"
    assert patch["item"]["title"] == "Ремонт телевизоров"
    assert patch["item"]["city_name"] == "Керчь"
    assert patch["item"]["city_tz"] == "Europe/Simferopol"


# --------------------------------------------------------------------------
# 4. Профиль клиента: ссылки нет, и это записано тестом
# --------------------------------------------------------------------------


def test_client_profile_url_is_never_invented() -> None:
    """ССЫЛКА ТОЛЬКО ТА, ЧТО ПРИСЛАЛ АВИТО. Из `external_id` она не выводится.

    ⚠ ЭТОТ ТЕСТ ЗАМЕНИЛ СОБОЙ `test_client_profile_url_is_none_on_purpose`, и
    заменил ОСОЗНАННО — как тот и требовал. Прежний охранял заготовку,
    возвращавшую `None` всегда: собрать адрес было не из чего, а собранный
    догадкой ведёт в 404. Условие снятия он назвал сам: «чинить можно только
    после того, как `client_enrich.peer_fields` покажет в логе боевое поле со
    ссылкой». Показал — 02.09, поле `public_user_profile`.

    Охраняемое правило при этом не ослабло, а стало точнее: ссылка не
    вычисляется НИКОГДА, ни из номера клиента, ни из чего-либо ещё, — она
    только читается из того, что Авито прислал и что прошло проверку.
    """
    from app.models import Client
    from app.services.client_enrich import client_profile_url

    # Номер клиента известен, ссылки нет — значит ссылки нет. Соблазн собрать
    # её из номера здесь и умирает: у публичного профиля другой ключ.
    assert client_profile_url(Client(channel="avito", external_id="923456789")) is None
    assert client_profile_url(None) is None

    сохранённая = Client(
        channel="avito",
        external_id="923456789",
        profile_url="https://avito.ru/user/0a1b2c3d4e5f60718293a4b5c6d7e8f9/profile",
    )
    assert client_profile_url(сохранённая) == сохранённая.profile_url


def test_peer_fields_are_logged_by_name_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В лог уезжают ИМЕНА полей собеседника и ни одного значения.

    Это единственный честный способ узнать, есть ли у Авито ссылка на профиль:
    через сутки боевого трафика список полей будет известен точно. Значения
    писать нельзя — там персональные данные живых людей.
    """
    from app.services import client_enrich

    spy = _LogSpy()
    monkeypatch.setattr(client_enrich, "log", spy)
    client_enrich._log_peer_fields(
        {
            "id": "u2i-1",
            "users": [
                {"id": 111222333, "name": "Мы"},
                {"id": 923456789, "name": "Иван Петров", "public_user_profile": {"url": "..."}},
            ],
        },
        111222333,
    )

    fields = spy.fields_of("client_enrich.peer_fields")["fields"]
    assert fields == ["id", "name", "public_user_profile"]
    dumped = repr(spy.calls)
    assert "Иван Петров" not in dumped
    assert "923456789" not in dumped


def test_peer_fields_logging_never_breaks_enrichment() -> None:
    """Диагностика не имеет права уронить задачу — что бы ни пришло."""
    from app.services.client_enrich import _log_peer_fields

    junk: list[Any] = [None, [], {"users": "нет"}, {"users": [None, 5]}, {}]
    for raw in junk:
        _log_peer_fields(raw, 111222333)


# --------------------------------------------------------------------------
# 5. Город доезжает до интерфейса
# --------------------------------------------------------------------------


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def conv_with_item(db_sessionmaker: Any, make_avito_account: Any) -> Any:
    account = await make_avito_account(avito_user_id=777888999)
    async with db_sessionmaker() as db:
        client_row = Client(channel="avito", external_id="923456789", name="Иван Петров")
        db.add(client_row)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-api-city",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            unread_count=0,
            item_title="Ремонт телевизоров",
            item_url=CANON,
            item_price="от 1 500 ₽",
            item_city_slug="kerch",
        )
        db.add(conv)
        await db.commit()
        return conv


async def test_api_gives_city_and_timezone(
    client: Any, tokens: dict[str, str], conv_with_item: Any
) -> None:
    """Деталь диалога несёт город и IANA-имя пояса.

    Именно имя, а не время и не смещение: время считает браузер, поэтому оно
    ИДЁТ, а не замирает на момент ответа сервера.
    """
    r = await client.get(f"/api/v1/conversations/{conv_with_item.id}", headers=auth(tokens))
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["city_name"] == "Керчь"
    assert item["city_tz"] == "Europe/Simferopol"
    assert item["title"] == "Ремонт телевизоров"


async def test_city_is_derived_from_url_when_column_is_empty(
    client: Any, tokens: dict[str, str], db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Диалог, записанный ДО выкатки: колонка пуста, а город всё равно виден.

    Поле, которое заполняется единственным путём, у половины строк остаётся
    пустым навсегда — это дефект 2 из docs/32. Здесь страховка: если колонки
    нет, город берётся из ссылки прямо на выдаче.
    """
    account = await make_avito_account(avito_user_id=777888998)
    async with db_sessionmaker() as db:
        client_row = Client(channel="avito", external_id="923456790", name="Пётр")
        db.add(client_row)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-legacy",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            unread_count=0,
            item_title="Ремонт стиральных машин",
            item_url="https://www.avito.ru/moskva/bytovaya_tehnika/remont_8213779976",
            item_city_slug=None,  # так выглядят все диалоги до 11 августа
        )
        db.add(conv)
        await db.commit()
        conv_id = conv.id

    r = await client.get(f"/api/v1/conversations/{conv_id}", headers=auth(tokens))
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["city_name"] == "Москва"
    assert item["city_tz"] == "Europe/Moscow"


async def test_api_gives_avito_id_and_empty_profile_url(
    client: Any, tokens: dict[str, str], conv_with_item: Any
) -> None:
    """Блок «Клиент на Авито»: идентификатор есть, ссылки нет и не выдумана."""
    r = await client.get(f"/api/v1/conversations/{conv_with_item.id}", headers=auth(tokens))
    body = r.json()["client"]
    assert body["external_id"] == "923456789"
    assert body["profile_url"] is None


async def test_conversation_without_item_has_no_city(
    client: Any, tokens: dict[str, str], seed_conversation: Any
) -> None:
    """u2u-чат без объявления: `item` пуст целиком, а не «город не определён»
    внутри пустого объявления."""
    url = f"/api/v1/conversations/{seed_conversation.conversation_id}"
    r = await client.get(url, headers=auth(tokens))
    assert r.json()["item"] is None
