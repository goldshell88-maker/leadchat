"""Яндекс Спеллер — опечатки в названии улицы до похода к карте.

ЗАЧЕМ (исследование 16.09). «улица Ленена 5», «праспект Мира 3» — карты и
ГАР-справочники на такое отвечают «не нашли», а стенд 30 дней даёт сотни
отказов `not_found`/`street_mismatch` ровно на опечатках. Спеллер — словарь
ОРФО: «Ленена» → «Ленина» одним вариантом. Топонимы он знает плохо
(«Салавье» → мусор), поэтому берём ТОЛЬКО однозначные замены, близкие к
исходному слову, и только в словах улицы — пункт и город не трогаем.

С 16.09 поход к Спеллеру и выбор варианта (`pick`) живут на шлюзе (docs/46,
`gateway/leadchat_gateway/providers/speller.py`); здесь — какие слова
спрашивать (`words_to_check`), кэш, счётчик и подстановка замен (`apply`).

Бесплатно: 10 000 обращений и 10 млн символов в сутки; коммерческое
использование разрешено при подписи «Проверка правописания: Яндекс.Спеллер»
(условия п. 3.1, 3.8) — подпись живёт в «О системе». Ключ не нужен.
Наружу уходят только слова улицы — без имён, номеров и домов.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError

PROVIDER = "speller"
#: Таймаут провайдера на шлюзе 4 с + запас на дорогу по мосту.
TIMEOUT_SEC = 6.0
#: Слова короче — не правим: «ул», «пр», «им» и двухбуквенные опечатки
#: словарь чинит куда угодно.
MIN_LEN = 5
#: Слова-типы улиц Спеллеру не отдаём: «ул», «пр-кт» он «исправит».
_ТИПЫ = frozenset(
    """ул улица пр пр-кт пр-т проспект пер переулок ш шоссе б-р бульвар наб набережная
пл площадь проезд туп тупик аллея линия тракт мкр мкр-н микрорайон кв-л квартал
снт днт тер территория массив дор дорога""".split()
)
_СЛОВО = re.compile(r"[А-ЯЁа-яё][А-ЯЁа-яё-]+")


def words_to_check(street: str) -> list[str]:
    """Слова улицы, которые стоит спросить: буквы, от MIN_LEN, не тип, не число."""
    return [
        w
        for w in _СЛОВО.findall(street or "")
        if len(w) >= MIN_LEN and w.lower() not in _ТИПЫ and "-" not in w
    ]


def apply(street: str, замены: dict[str, str]) -> str:
    """Подставить замены в улицу, сохраняя регистр первой буквы."""
    out = street
    for было, стало in замены.items():
        if было and было[0].isupper():
            стало = стало[:1].upper() + стало[1:]

        # Замена — текстом, не шаблоном: обратная косая в ответе словаря не
        # должна читаться как группа `re.sub`.
        def подставить(_m: re.Match[str], з: str = стало) -> str:
            return з

        out = re.sub(rf"(?<![а-яёА-ЯЁ]){re.escape(было)}(?![а-яёА-ЯЁ])", подставить, out)
    return out


def _замены(fixes: Any) -> dict[str, str]:
    """`fixes` шлюза (или кэша) → словарь слово→замена; иная форма — сбой."""
    if not isinstance(fixes, dict) or not all(
        isinstance(к, str) and isinstance(з, str) for к, з in fixes.items()
    ):
        raise GeocodeError(PROVIDER, "bad_response")
    return fixes


async def fix_street(
    street: str,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """Улица с исправленными опечатками — или None, если править нечего.

    `on_request` — счётчик походов (суточный потолок 10 000 считает воркер);
    зовётся только перед настоящим походом в шлюз, не при попадании в кэш.
    """
    слова = words_to_check(street)
    if not слова:
        return None
    тело = {"text": " ".join(слова), "words": слова}
    для_кэша = {"v": 2, **тело}
    из_кэша = await geo_cache.get(PROVIDER, для_кэша)
    if из_кэша is not None:
        замены = _замены(из_кэша)
        return apply(street, замены) if замены else None
    if on_request is not None:
        await on_request()
    try:
        данные = await gateway.call(
            "/spell", тело, timeout=TIMEOUT_SEC, client=client, provider=PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc
    замены = _замены(данные.get("fixes"))
    await geo_cache.put(PROVIDER, для_кэша, замены, empty=not замены)
    return apply(street, замены) if замены else None
