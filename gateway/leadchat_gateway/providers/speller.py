"""Яндекс Спеллер — опечатки в названии улицы до похода к карте.

ЗАЧЕМ (исследование 16.09). «улица Ленена 5», «праспект Мира 3» — карты и
ГАР-справочники на такое отвечают «не нашли». Спеллер — словарь ОРФО:
«Ленена» → «Ленина» одним вариантом. Топонимы он знает плохо («Салавье» →
мусор), поэтому берём ТОЛЬКО однозначные замены, близкие к исходному слову.

Граница с LeadChat: КАКИЕ слова спрашивать и как подставить замены в улицу
решает LeadChat (`words_to_check`, `apply`); здесь — поход к Спеллеру и выбор
варианта (`pick`). Сюда приходят только слова улицы — без имён и домов.

Бесплатно: 10 000 обращений и 10 млн символов в сутки; ключ не нужен.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from leadchat_gateway import http
from leadchat_gateway.errors import ProviderError

PROVIDER = "speller"
BASE_URL = "https://speller.yandex.net/services/spellservice.json/checkText"
TIMEOUT = httpx.Timeout(4.0, connect=2.0)
#: Замена не дальше двух правок от слова клиента: «Ленена»→«Ленина» — одна,
#: «Салавье»→«Соловье» — две, но топонимы дальше этого — уже другое слово.
MAX_DISTANCE = 2
_СЛОВО = re.compile(r"[А-ЯЁа-яё][А-ЯЁа-яё-]+")


def _расстояние(а: str, б: str) -> int:
    """Расстояние Дамерау—Левенштейна: правок между словами, перестановка
    соседних букв («Амнудсена» ← «Амундсена») — одна правка, как в жизни.
    Копия `app/services/geocode.py` LeadChat: шлюз от `app.*` не зависит."""
    if а == б:
        return 0
    d = [[0] * (len(б) + 1) for _ in range(len(а) + 1)]
    for i in range(len(а) + 1):
        d[i][0] = i
    for j in range(len(б) + 1):
        d[0][j] = j
    for i in range(1, len(а) + 1):
        for j in range(1, len(б) + 1):
            цена = 0 if а[i - 1] == б[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + цена)
            if i > 1 and j > 1 and а[i - 1] == б[j - 2] and а[i - 2] == б[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(а)][len(б)]


def pick(данные: Any, слова: list[str]) -> dict[str, str]:
    """Ответ Спеллера → замены, которым верим: первый вариант близок к слову, а
    второй — не ближе первого (иначе словарь колеблется, и выбирать не нам).

    Формат: `[{"word": "Ленена", "s": ["Ленина", "Ленька"], "pos": 6, "len": 6,
    "code": 1}]`; варианты идут по убыванию уверенности словаря. `code` 1 —
    ошибка в слове (другие коды — повтор, регистр — не наши).
    """
    if not isinstance(данные, list):
        raise ProviderError("bad_response", None, "ответ не список")
    замены: dict[str, str] = {}
    искомые = {w.lower(): w for w in слова}
    for ошибка in данные:
        if not isinstance(ошибка, dict) or ошибка.get("code") not in (1, None):
            continue
        слово = str(ошибка.get("word") or "")
        варианты = [
            str(v)
            for v in (ошибка.get("s") or [])
            if isinstance(v, str) and " " not in v and _СЛОВО.fullmatch(v)
        ]
        if слово.lower() not in искомые or not варианты:
            continue
        вариант = варианты[0]
        if вариант.lower() == слово.lower():
            continue
        близость = _расстояние(слово.lower(), вариант.lower())
        if близость > MAX_DISTANCE:
            continue
        if len(варианты) > 1 and _расстояние(слово.lower(), варианты[1].lower()) < близость:
            # Второй вариант ближе первого — словарь колеблется, не наш выбор.
            # Равно близкие — берём первый: он у словаря увереннее, а карта
            # всё равно обязана подтвердить исправленную улицу.
            continue
        замены[искомые[слово.lower()]] = вариант
    return замены


async def check(text: str, words: list[str]) -> dict[str, str]:
    """Один GET к Спеллеру: `text` — строка на проверку, `words` — какие из её
    слов LeadChat готов заменить (в исходном регистре, для ключей ответа)."""
    params = {"text": text, "lang": "ru", "format": "plain"}
    try:
        async with http.client(TIMEOUT) as own:
            resp = await own.get(BASE_URL, params=params)
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    http.check_status(resp)
    return pick(http.parse_json(resp), words)
