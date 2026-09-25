"""Пакет 7a, Q16 (21.09): стоп-классы речи по форме, классы (1)–(11).

Программа «автоматически и точно» §2.1, договор `contract_7a.md` §A:

* одиннадцать имён в реестре `address_parse.PARSE_RULES` с умолчанием `off`;
  при `off`/`rules=None` разбор байт в байт прежний (сторожа — корпус 1209,
  золотой корпус, `test_реестр_пуст_поведение_как_сегодня`);
* `on` — ложная форма снимается: `parse` молчит, ворота `gate` закрыты, и
  модель-читатель не будится (`looks_like_address` → False, М-10); контрпримеры
  живут как без правила;
* `shadow` — строка та же, след `trace.stop_shadow=имя`;
* класс судит невод ворот (`_gate`) и ГОЛОВУ РАЗБОРА (`parse` → `under_rule`):
  «Обычная 2х комнатная квартира» и «Хочу 10 … офис 2024» — уровень A через
  часть адреса, невод их не решал;
* класс (8) `STOP_C_ONE_WORD_FORM` — только уровень C: после вопроса
  оператора «Танк 581» — ответ, его судит карта; топонимы в родительном
  («Мира», «Победы», «Октября») и пункты («Разумное», «Майский») выживают;
* класс (2) не трогает «Ленина 6 м» — литера через пробел, дом решает `_ДОМ`;
* каждый класс включается и меряется отдельно: `off` соседа впереди по
  списку не заслоняет `on` («Квадратов 80» — единица и величина разом);
* `stopped_by` — имя класса в `on` для журнала `address.stop_class` в
  `inbound._maybe_extract_address`: одна строка на реплику, без слов клиента.

Ожидания «как сегодня» сняты ПРОГОНОМ 21.09 (`uv run python`, карта
`map7a_q16.md` §5), не выдуманы. Реплики вымышленные или формы из корзин
стенда без телефонов и имён.

ДИВЕРСИИ (каждая прогнана 21.09 «правка → красный → обратный Edit», хеш
файла сверен):
1. `_gate` не закрывает ворота при `on` (убрать `return None, имя`) —
   краснеют `test_ложное_под_политикой[*]` по `gate`/`looks_like_address`;
2. `_класс_под_политикой` снимает при чистой голове рядом (убрать `any(not
   классы …)`) — краснеет `test_чистая_голова_рядом_держит_ворота`;
3. класс (8) судит и на B (`{LEVEL_B, LEVEL_C}`) — краснеет
   `test_класс_8_только_уровень_C` и `test_ложное_под_политикой[STOP_C_ONE_WORD_FORM-*]`;
4. класс (2) снимает по голому метру без величины в голове — краснеет
   `test_контрпример_живёт[STOP_BARE_METRE-Ленина 6 м]`;
5. `parse` без `under_rule` по голове разбора (вернуть `Found` как есть) —
   краснеют `test_голова_разбора_строчными_на_A` и тень в
   `test_ложное_под_политикой[STOP_ORDINAL_SUFFIX-Обычная 2х…]`;
6. `stopped_by` отдаёт имя и в `shadow` (`!= PARSE_OFF` вместо `== PARSE_ON`)
   — краснеет `test_stopped_by_только_on`;
7. родительный падеж не исключён из класса (8) (убрать `а|я|ы|и|ов|ев|ей` из
   `_ФОРМА_НАЗВАНИЯ`) — краснеют `test_контрпример_живёт[STOP_C_ONE_WORD_FORM-Мира 10]`
   и соседи;
8. имена не в реестре (убрать `PARSE_RULES.update`) — краснеет
   `test_реестр_одиннадцать_имён_off` и все `on`-проверки;
9. `inbound` без журнала (убрать `log.info("address.stop_class", …)`) —
   краснеет `test_inbound_журнал_один_раз_на_реплику`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.models import ClientAddressCandidate, Conversation
from app.services import address_llm, address_parse, app_settings
from app.services.address_parse import (
    STOP_ADVERB,
    STOP_BARE_METRE,
    STOP_C_ONE_WORD_FORM,
    STOP_DATE_DOT,
    STOP_DIGITS_WORD,
    STOP_IMPERATIVE,
    STOP_LATIN_BRAND,
    STOP_LIST_MARKERS,
    STOP_ORDINAL_SUFFIX,
    STOP_PRONOUN,
    STOP_UNIT_BEFORE_NUMBER,
    СТОП_КЛАССЫ,
)
from app.services.inbound import apply_inbound_event
from tests.unit import test_paket2_history_1909 as история

pytestmark = pytest.mark.anyio

без_сети = история.без_сети
NOW = datetime.now(UTC).replace(microsecond=0)
ВСЕ_ON = dict.fromkeys(СТОП_КЛАССЫ, address_parse.PARSE_ON)


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(история.ACCOUNT_UID)


def _вид(f: address_parse.Found | None) -> tuple[str, str] | None:
    return None if f is None else (f.level, f.value)


def _без_следа(f: address_parse.Found | None) -> address_parse.Found | None:
    return None if f is None else dataclasses.replace(f, trace=None)


# ── ложные формы: имя класса → (текст, сегодня без вопроса, сегодня после вопроса)

ЛОЖНЫЕ: dict[str, list[tuple[str, tuple[str, str] | None, tuple[str, str] | None]]] = {
    STOP_UNIT_BEFORE_NUMBER: [
        ("Метра 4", ("C", "Метра, 4"), ("B", "Метра, 4")),
        ("Лежал года 2", ("C", "Лежал года, 2"), ("B", "Лежал года, 2")),
        ("Числа 23 займусь", ("C", "Числа, 23"), ("B", "Числа, 23")),
        ("Раза 4", ("C", "Раза, 4"), ("B", "Раза, 4")),
        ("Мин 3", ("C", "Мин, 3"), ("B", "Мин, 3")),
        ("Обжать 3 провода", ("C", "Обжать, 3"), ("B", "Обжать, 3")),
        ("Просверлить 10", ("C", "Просверлить, 10"), ("B", "Просверлить, 10")),
    ],
    STOP_BARE_METRE: [
        ("Расстояние 5 м до столба", ("C", "Расстояние, 5"), ("B", "Расстояние, 5")),
        ("Верх 3м", ("C", "Верх, 3м"), ("B", "Верх, 3м")),
        ("Стена 4м", ("C", "Стена, 4м"), ("B", "Стена, 4м")),
        # Строчными — только в ответ на вопрос оператора (невод B).
        ("прокладка 100м", None, ("B", "прокладка, 100м")),
        ("Квадратов 80", ("C", "Квадратов, 80"), ("B", "Квадратов, 80")),
        ("Бетон 3 м3", ("C", "Бетон, 3"), ("B", "Бетон, 3")),
        ("Площадь 60 квадратов", ("C", "Площадь, 60"), ("B", "Площадь, 60")),
        ("Комнату 7квадратов", ("C", "Комнату, 7"), ("B", "Комнату, 7")),
    ],
    STOP_LATIN_BRAND: [
        ("Хонор 8х", ("C", "Хонор, 8х"), ("B", "Хонор, 8х")),
        ("Танк 581", ("C", "Танк, 581"), ("B", "Танк, 581")),
        ("Асус -11", ("C", "Асус, 11"), ("B", "Асус, 11")),
        ("Эпсон 412", ("C", "Эпсон, 412"), ("B", "Эпсон, 412")),
        # Невод берёт с «Джи», марка — целый токен.
        ("ЛДжи 126", ("C", "ЛДжи, 126"), ("B", "ЛДжи, 126")),
        # Невод берёт «Нова 13», марка — слово перед ним.
        ("Хуавей Нова 13", ("C", "Хуавей Нова, 13"), ("B", "Хуавей Нова, 13")),
        ("перепрошивкой 4 g модема", None, ("B", "перепрошивкой, 4")),
        # «д16» после марки — не «дом 16».
        ("Смарт таб д16", ("C", "Смарт таб, 16"), ("B", "Смарт таб, 16")),
    ],
    STOP_ORDINAL_SUFFIX: [
        ("Сакура 2-х створчатый", ("C", "Сакура, 2"), ("B", "Сакура, 2")),
        ("Грандинс 4-х ств", ("C", "Грандинс, 4"), ("B", "Грандинс, 4")),
        # Уровень A через «квартира»: класс судит и при названной части.
        ("Обычная 2х комнатная квартира", ("A", "Обычная, 2х"), ("A", "Обычная, 2х")),
    ],
    STOP_LIST_MARKERS: [
        # «2. Замена выключателя 3.» с 25.09 снимает стоп-список родительного
        # (`_НЕ_УЛИЦА`) — образец проверял бы его, а не класс; голова та же по
        # форме, но не мебель и не техника.
        (
            "1. Замена люстры 2. Демонтаж плитки 3. Установка розетки",
            ("C", "Демонтаж плитки, 3"),
            ("B", "Демонтаж плитки, 3"),
        ),
        ("2. Два столика 3. Две тумбы", ("C", "Два столика, 3"), ("B", "Два столика, 3")),
        (
            "1. Нажмите кнопку 2. Откройте Настройки 3. Выберите сеть",
            ("C", "Откройте Настройки, 3"),
            ("B", "Откройте Настройки, 3"),
        ),
    ],
    STOP_DIGITS_WORD: [
        ("Последние цифры 20-84", ("C", "Последние цифры, 20"), ("B", "Последние цифры, 20")),
        ("Цифры 12", ("C", "Цифры, 12"), ("B", "Цифры, 12")),
    ],
    STOP_C_ONE_WORD_FORM: [
        ("Танк 581", ("C", "Танк, 581"), ("B", "Танк, 581")),
        ("Винду 10", ("C", "Винду, 10"), ("B", "Винду, 10")),
        ("Даже 21", ("C", "Даже, 21"), ("B", "Даже, 21")),
        ("Архикад 26", ("C", "Архикад, 26"), ("B", "Архикад, 26")),
        ("Память 128", ("C", "Память, 128"), ("B", "Память, 128")),
        ("Простите,599", ("C", "Простите, 599"), ("B", "Простите, 599")),
    ],
    STOP_IMPERATIVE: [
        ("Наберите 2гис", ("C", "Наберите, 2"), ("B", "Наберите, 2")),
        # Невод берёт «Настройки 3», глагол — слово перед ним.
        ("Откройте Настройки 3", ("C", "Откройте Настройки, 3"), ("B", "Откройте Настройки, 3")),
        ("Напишите 10", ("C", "Напишите, 10"), ("B", "Напишите, 10")),
        ("Приезжайте 15", ("C", "Приезжайте, 15"), ("B", "Приезжайте, 15")),
        ("Скиньте 2 фото", ("C", "Скиньте, 2"), ("B", "Скиньте, 2")),
        ("Смотрите 3", ("C", "Смотрите, 3"), ("B", "Смотрите, 3")),
    ],
    STOP_ADVERB: [
        ("Приблизительно 75", ("C", "Приблизительно, 75"), ("B", "Приблизительно, 75")),
        ("Желательно 11", ("C", "Желательно, 11"), ("B", "Желательно, 11")),
        ("Желательно 10 винда", ("C", "Желательно, 10"), ("B", "Желательно, 10")),
        ("Ориентировочно 5", ("C", "Ориентировочно, 5"), ("B", "Ориентировочно, 5")),
        ("Реально 3", ("C", "Реально, 3"), ("B", "Реально, 3")),
        ("Максимально 10", ("C", "Максимально, 10"), ("B", "Максимально, 10")),
        # Разбор и так молчит («обычно» служебное), но ворота были открыты и
        # будили модель (М-10, карта §2.2 «Мигает 60»).
        ("Обычно 2", None, None),
    ],
    STOP_PRONOUN: [
        ("Все 4", ("C", "Все, 4"), ("B", "Все, 4")),
        ("все 4 положения", None, ("B", "все, 4")),
        ("Хочу 10", ("C", "Хочу, 10"), ("B", "Хочу, 10")),
        ("Ему 26", ("C", "Ему, 26"), ("B", "Ему, 26")),
        ("Другие 20", ("C", "Другие, 20"), ("B", "Другие, 20")),
        ("Каждый 3", ("C", "Каждый, 3"), ("B", "Каждый, 3")),
        ("остальные 2", None, ("B", "остальные, 2")),
        # Уровень A через «офис 2024» — класс судит и при названной части.
        ("Хочу 10 винду в офис 2024", ("A", "Хочу, 10"), ("A", "Хочу, 10")),
    ],
}

# ── контрпримеры: имя класса → тексты, обязанные жить при этом классе в `on`

КОНТРПРИМЕРЫ: dict[str, list[str]] = {
    STOP_UNIT_BEFORE_NUMBER: [
        "50 лет Октября 5",
        "8 Марта 12",
        "40 лет Октября 5",
        "Млечный Путь 5",
        "Юность 5",
        # Прошедшее время класс не берёт — договор: оставить как есть.
        "Печатали 100 страниц",
    ],
    STOP_BARE_METRE: ["Ленина 6м", "Ленина 6М", "Ленина 6 м", "Кабель 50 м"],
    STOP_LATIN_BRAND: ["Октября 85 х", "85 х"],
    STOP_ORDINAL_SUFFIX: ["20-летия Октября 5", "Ленина 2 к 3", "Ленина 5, 2-х комнатная"],
    STOP_LIST_MARKERS: ["1. Ленина 5 2. кв 3", "1. Ленина 5 2. Мира 3"],
    STOP_DATE_DOT: ["Мира 4 21.09", "ул Ленина 5.10 подъезд 2"],
    STOP_DIGITS_WORD: ["Мира 10 на 15:00"],
    STOP_C_ONE_WORD_FORM: [
        "Ясная 24",
        "Вольская 5",
        "Мира 10",
        "Победы 56",
        "Октября 30",
        "Кирова 7",
        "Гагарина 14б",
        "Строителей 12",
        "Революции 15",
        "Разумное 81",
        "Майский 80",
        "Иваново 12",
        "Ангарск 94",
        # Одна цифра — вне класса.
        "Юник 5",
    ],
    STOP_IMPERATIVE: [
        "Ясная 24",
        "Вольская 5",
        "Ключевая 12",
        "Приезжайте на Ленина 5",
        # За глаголом улица в форме прилагательного — адрес.
        "Приезжайте Ленина 5",
    ],
    STOP_ADVERB: ["Центральная 5", "Октябрьская 3"],
    STOP_PRONOUN: ["Вольская 5", "Мира 5"],
}

ЛОЖНЫЕ_ПЛОСКО = [(имя, *строка) for имя, строки in ЛОЖНЫЕ.items() for строка in строки]
КОНТРПРИМЕРЫ_ПЛОСКО = [(имя, т) for имя, тексты in КОНТРПРИМЕРЫ.items() for т in тексты]


# ── реестр ───────────────────────────────────────────────────────────────────


def test_реестр_одиннадцать_имён_off() -> None:
    """Одиннадцать имён договора — в реестре, умолчание `off`; настройка
    перекрывает поимённо, порядок — порядок договора."""
    assert СТОП_КЛАССЫ == (
        STOP_UNIT_BEFORE_NUMBER,
        STOP_BARE_METRE,
        STOP_LATIN_BRAND,
        STOP_ORDINAL_SUFFIX,
        STOP_LIST_MARKERS,
        STOP_DATE_DOT,
        STOP_DIGITS_WORD,
        STOP_C_ONE_WORD_FORM,
        STOP_IMPERATIVE,
        STOP_ADVERB,
        STOP_PRONOUN,
    )
    for имя in СТОП_КЛАССЫ:
        assert address_parse.PARSE_RULES[имя] == address_parse.PARSE_OFF, имя
        assert address_parse.rule_state(None, имя) == address_parse.PARSE_OFF
    политика = address_parse.rules_from_setting("STOP_PRONOUN=on; STOP_ADVERB = Shadow")
    assert (политика[STOP_PRONOUN], политика[STOP_ADVERB]) == ("on", "shadow")
    assert all(
        политика[имя] == "off" for имя in СТОП_КЛАССЫ if имя not in (STOP_PRONOUN, STOP_ADVERB)
    )
    with pytest.raises(ValueError, match="неизвестное правило разбора"):
        address_parse.rules_from_setting("STOP_NOPE=on")


# ── (а)–(г) по каждой ложной форме ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("имя", "текст", "сегодня_C", "сегодня_B"),
    ЛОЖНЫЕ_ПЛОСКО,
    ids=[f"{имя}-{т}" for имя, т, _, _ in ЛОЖНЫЕ_ПЛОСКО],
)
def test_ложное_под_политикой(
    имя: str, текст: str, сегодня_C: tuple[str, str] | None, сегодня_B: tuple[str, str] | None
) -> None:
    on, shadow, off = {имя: "on"}, {имя: "shadow"}, {имя: "off"}
    # (б) без правила и в `off` — как сегодня, байт в байт.
    assert _вид(address_parse.parse(текст)) == сегодня_C
    assert _вид(address_parse.parse(текст, про_адрес=True)) == сегодня_B
    assert address_parse.parse(текст, rules=off) == address_parse.parse(текст)
    assert address_parse.parse(текст, про_адрес=True, rules=off) == address_parse.parse(
        текст, про_адрес=True
    )
    assert address_parse.gate(текст, rules=off) == address_parse.gate(текст)
    # (а) в `on` — снято: разбор молчит, ворота закрыты, модель не будится.
    assert address_parse.parse(текст, rules=on) is None
    assert address_parse.gate(текст, rules=on) is None
    assert address_llm.looks_like_address(текст, rules=on) is False
    if имя == STOP_C_ONE_WORD_FORM:
        # Только уровень C: после вопроса оператора — как сегодня.
        assert _вид(address_parse.parse(текст, про_адрес=True, rules=on)) == сегодня_B
        assert address_parse.gate(текст, про_адрес=True, rules=on) == address_parse.gate(
            текст, про_адрес=True
        )
    else:
        assert address_parse.parse(текст, про_адрес=True, rules=on) is None
        assert address_parse.gate(текст, про_адрес=True, rules=on) is None
    # (в) в `shadow` — строка та же, след `stop_shadow`.
    for про_адрес, сегодня in ((False, сегодня_C), (True, сегодня_B)):
        тень = address_parse.parse(текст, про_адрес=про_адрес, rules=shadow)
        if сегодня is None:
            assert тень is None
        elif про_адрес and имя == STOP_C_ONE_WORD_FORM:
            # Класс на B не судит: ни снятия, ни следа.
            assert тень == address_parse.parse(текст, про_адрес=True)
        else:
            assert тень is not None and тень.trace == {"stop_shadow": имя}, тень
            assert _без_следа(тень) == address_parse.parse(текст, про_адрес=про_адрес)
    # (г) `stopped_by` — имя при `on`; строчные формы — только после вопроса.
    про_адрес = address_parse.gate(текст) is None
    assert address_parse.stopped_by(текст, on, про_адрес=про_адрес) == имя
    assert address_parse.stopped_by(текст, shadow, про_адрес=про_адрес) is None
    assert address_parse.stopped_by(текст, None, про_адрес=про_адрес) is None


@pytest.mark.parametrize(
    ("имя", "текст"), КОНТРПРИМЕРЫ_ПЛОСКО, ids=[f"{имя}-{т}" for имя, т in КОНТРПРИМЕРЫ_ПЛОСКО]
)
def test_контрпример_живёт(имя: str, текст: str) -> None:
    """Контрпример при своём классе в `on` — как без правила: разбор, ворота,
    модель; в `shadow` — без следа."""
    on = {имя: "on"}
    for про_адрес in (False, True):
        assert address_parse.parse(текст, про_адрес=про_адрес, rules=on) == address_parse.parse(
            текст, про_адрес=про_адрес
        ), текст
        assert address_parse.gate(текст, про_адрес=про_адрес, rules=on) == address_parse.gate(
            текст, про_адрес=про_адрес
        )
        тень = address_parse.parse(текст, про_адрес=про_адрес, rules={имя: "shadow"})
        assert тень == address_parse.parse(текст, про_адрес=про_адрес)
        assert тень is None or not (тень.trace or {}).get("stop_shadow")
    assert address_llm.looks_like_address(текст, rules=on) is address_llm.looks_like_address(текст)
    assert address_parse.stopped_by(текст, on) is None


# ── устройство: уровни, головы, политика по классам ──────────────────────────


def test_класс_8_только_уровень_C() -> None:
    """«Танк 581» без вопроса — речь уровня C; после «куда подъехать?» — ответ
    уровня B, класс (8) его не судит (карта решит)."""
    on = {STOP_C_ONE_WORD_FORM: "on"}
    assert address_parse.parse("Танк 581", rules=on) is None
    после = address_parse.parse("Танк 581", про_адрес=True, rules=on)
    assert после is not None and (после.level, после.value) == ("B", "Танк, 581")
    assert address_parse.speech_stop_class("Танк 581", после_вопроса=False, уровень="C") in (
        STOP_LATIN_BRAND,
        STOP_C_ONE_WORD_FORM,
    )
    assert address_parse.speech_stop_class("Винду 10", после_вопроса=True, уровень="B") is None


def test_чистая_голова_рядом_держит_ворота() -> None:
    """Ворота закрывает только реплика, в которой речью оказалась КАЖДАЯ
    голова: «Все 4 розетки, ул Ленина 5» — адрес, модель будить можно. Голову
    разбора класс судит отдельно: «Обжать 3 провода, Ленина 5» — разбор берёт
    «Обжать 3» (как и сегодня), и эту строку класс снимает; ворота открыты."""
    адрес = "Все 4 розетки поменять, ул Ленина 5"
    f = address_parse.parse(адрес, rules=ВСЕ_ON)
    assert f is not None and (f.level, f.value) == ("A", "ул Ленина, 5")
    assert f.trace is None
    assert address_parse.gate(адрес, rules=ВСЕ_ON) == "A"
    assert address_llm.looks_like_address(адрес, rules=ВСЕ_ON) is True
    assert address_parse.speech_stop_class(адрес, после_вопроса=False, уровень="A") is None

    речь_первой = "Обжать 3 провода, Ленина 5"
    сегодня = address_parse.parse(речь_первой)
    assert сегодня is not None and сегодня.value == "Обжать, 3"
    assert address_parse.gate(речь_первой, rules=ВСЕ_ON) == "C"
    assert address_parse.parse(речь_первой, rules=ВСЕ_ON) is None
    тень = address_parse.parse(речь_первой, rules={STOP_UNIT_BEFORE_NUMBER: "shadow"})
    assert тень is not None and тень.trace == {"stop_shadow": STOP_UNIT_BEFORE_NUMBER}
    assert address_parse.stopped_by(речь_первой, ВСЕ_ON) == STOP_UNIT_BEFORE_NUMBER


def test_голова_разбора_строчными_на_A() -> None:
    """Невод строчных без вопроса не видит, а уровень A даёт «квартира»:
    класс судит голову разбора; ворота для модели при этом открыты (как
    сегодня), имя для журнала берётся из следа тени."""
    текст = "обычная 2х комнатная квартира"
    сегодня = address_parse.parse(текст)
    assert сегодня is not None and (сегодня.level, сегодня.value) == ("A", "обычная, 2х")
    on = {STOP_ORDINAL_SUFFIX: "on"}
    assert address_parse.parse(текст, rules=on) is None
    assert address_parse.gate(текст, rules=on) == "A"
    тень = address_parse.parse(текст, rules={STOP_ORDINAL_SUFFIX: "shadow"})
    assert тень is not None and тень.trace == {"stop_shadow": STOP_ORDINAL_SUFFIX}
    assert address_parse.stopped_by(текст, on) == STOP_ORDINAL_SUFFIX


def test_каждый_класс_включается_отдельно() -> None:
    """«Квадратов 80» снимают и единица (1), и величина (2). Класс `off`
    впереди по списку не заслоняет `on` соседа; в тени след — от того, кто в
    тени; при равных — первый по договору."""
    т = "Квадратов 80"
    assert address_parse.speech_stop_class(т, после_вопроса=False, уровень="C") == (
        STOP_UNIT_BEFORE_NUMBER
    )
    assert address_parse.parse(т, rules={STOP_BARE_METRE: "on"}) is None
    assert address_parse.stopped_by(т, {STOP_BARE_METRE: "on"}) == STOP_BARE_METRE
    assert address_parse.parse(т, rules={STOP_UNIT_BEFORE_NUMBER: "on"}) is None
    тень = address_parse.parse(т, rules={STOP_BARE_METRE: "shadow"})
    assert тень is not None and тень.trace == {"stop_shadow": STOP_BARE_METRE}
    тень = address_parse.parse(т, rules={STOP_UNIT_BEFORE_NUMBER: "shadow", STOP_BARE_METRE: "on"})
    assert тень is None
    тень = address_parse.parse(
        т, rules={STOP_UNIT_BEFORE_NUMBER: "shadow", STOP_BARE_METRE: "shadow"}
    )
    assert тень is not None and тень.trace == {"stop_shadow": STOP_UNIT_BEFORE_NUMBER}


def test_класс_6_имя_ради_реестра() -> None:
    """Дата «д.ММ» на пути A закрыта кодом 21.09, а невод B/C дробь за числом
    не берёт: класс (6) по построению не срабатывает — имя живёт в реестре
    ради настройки и следа, поведение при любой политике одно."""
    т = "монтаж декоративных конструкций, 3.09"
    for политика in ("off", "shadow", "on"):
        assert address_parse.parse(т, rules={STOP_DATE_DOT: политика}) is None
        assert address_parse.parse(т, про_адрес=True, rules={STOP_DATE_DOT: политика}) is None
        assert address_parse.gate(т, rules={STOP_DATE_DOT: политика}) is None
    assert address_parse.stopped_by(т, {STOP_DATE_DOT: "on"}) is None
    assert address_parse.rules_from_setting("STOP_DATE_DOT=on")[STOP_DATE_DOT] == "on"


def test_известные_промахи_закреплены() -> None:
    """Что класс намеренно НЕ берёт или берёт лишнее — закреплено фактом,
    чтобы правка формы не прошла мимо: «Реалии 10» живёт (родительный на «и»
    — «Революции 15», «Юности 5»); «Приезжайте Мира 5» без предлога снимается
    (за глаголом не прилагательное), с предлогом — адрес."""
    ж = address_parse.parse("Реалии 10", rules=ВСЕ_ON)
    assert ж is not None and ж.value == "Реалии, 10"
    assert address_parse.parse("Приезжайте Мира 5", rules=ВСЕ_ON) is None
    м = address_parse.parse("Приезжайте на Мира 5", rules=ВСЕ_ON)
    assert м is not None and м.value == "Мира, 5"


def test_stopped_by_только_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stopped_by` — имя только в `on`; без единого включённого класса —
    сразу `None`, без единого разбора (в бою зовётся на каждое входящее без
    строки)."""
    вызовы: list[str] = []
    исходный = address_parse._gate

    def шпион(*a: Any, **kw: Any) -> Any:
        вызовы.append("gate")
        return исходный(*a, **kw)

    monkeypatch.setattr(address_parse, "_gate", шпион)
    assert address_parse.stopped_by("Хочу 10", None) is None
    assert address_parse.stopped_by("Хочу 10", dict.fromkeys(СТОП_КЛАССЫ, "shadow")) is None
    assert not вызовы
    assert address_parse.stopped_by("Хочу 10", {STOP_PRONOUN: "on"}) == STOP_PRONOUN
    assert вызовы == ["gate"]


def test_ворота_модели_под_классом() -> None:
    """М-10: у ложной формы с заглавной ворота модели открыты (`True`), под
    классом в `on` — закрыты; клиентское «адрес» словом ворота держит и под
    классом."""
    for имя, строки in ЛОЖНЫЕ.items():
        for текст, _, _ in строки:
            if address_parse.gate(текст) is None:
                continue
            assert address_llm.looks_like_address(текст) is True, текст
            assert address_llm.looks_like_address(текст, rules={имя: "on"}) is False, текст
    assert address_llm.looks_like_address("Хочу 10, адрес скину", rules=ВСЕ_ON) is True


# ── inbound: журнал один раз на реплику ──────────────────────────────────────


async def _политика_в_базе(db_sessionmaker: Any, текст: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_PARSE_RULES: текст}, user_id=None)
        await s.commit()


async def _строки(db_sessionmaker: Any) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as s:
        return list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())


async def test_inbound_журнал_один_раз_на_реплику(
    db: Any,
    redis: Any,
    account: Any,
    db_sessionmaker: Any,
    без_сети: Any,
) -> None:
    """Класс в `on`: строки нет, одна запись `address.stop_class` на реплику
    (`parse` зовётся ≥ 5 раз на входящее — счётчик там врал бы), с именем
    класса, уровнем до класса и без слов клиента. В `off` — строка C как
    сегодня и журнала нет."""
    await _политика_в_базе(db_sessionmaker, f"{STOP_PRONOUN}=on")
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(db, redis, account, история._live("Хочу 10", msg="m-1", when=NOW))
    записи = [л for л in логи if л["event"] == "address.stop_class"]
    assert len(записи) == 1, логи
    (запись,) = записи
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "chat-n29")
            )
        ).scalar_one()
    assert (запись["rule"], запись["level"], запись["state"]) == (STOP_PRONOUN, "C", "on")
    assert запись["conversation_id"] == str(conv.id) and запись["client_id"] == str(conv.client_id)
    assert "Хочу" not in str(запись)
    assert await _строки(db_sessionmaker) == []

    await _политика_в_базе(db_sessionmaker, f"{STOP_PRONOUN}=off")
    with structlog.testing.capture_logs() as логи:
        await apply_inbound_event(db, redis, account, история._live("Хочу 10", msg="m-2", when=NOW))
    assert not [л for л in логи if л["event"] == "address.stop_class"]
    assert [(r.level, r.value) for r in await _строки(db_sessionmaker)] == [("C", "Хочу, 10")]
