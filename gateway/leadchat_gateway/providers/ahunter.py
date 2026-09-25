"""Ahunter («Охотник за адресами») — второй справочник ГАР, без ключа и потолка.

ЗАЧЕМ (исследование 16.09). DaData — 10 000 запросов в сутки на все методы,
и к вечеру догона её нет; Яндекс Саджест — 1 000 в сутки и чужая лицензия.
Ahunter отдаёт подсказки по тому же ГАР одной строкой, без регистрации и
без суточного потолка, знает кварталы Ангарска («кв-л 57, дом 8»), корпуса
Зеленограда («г Зеленоград, корп 1218»), СНТ и территории. Координат в
анонимном ответе нет — это ТЕКСТ: исправленная улица, дом и пункт, с которыми
LeadChat снова идёт к карте за координатами и вердиктом.

Ответ `suggest/address?output=json;query=…` (параметры через «;»):
`{"suggestions": [{"value": "обл Иркутская, г Ангарск, кв-л 57, дом 8",
"machine": "…", "sign": "…", "zip": "…"}]}`. Разбираем `value`: куски через
запятую, у каждого тип-сокращение впереди («г», «р-н», «кв-л», «дом»).

⚠ СЕРВИС ЖИВЁТ НА ОДНОМ СЕРВЕРЕ И БЕЗ SLA: из России отвечает за
миллисекунды, из-за рубежа может не отвечать вовсе. Таймаут короткий; любой
отказ (сеть, 403, не JSON) LeadChat кладёт на десять минут у себя (`_ahunter_прилёг`
в воркере). Сюда приходит только собранная адресная строка — город, пункт,
улица и дом из разбора, без имён и телефонов.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from leadchat_gateway import http
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.types import SuggestedAhunter

PROVIDER = "ahunter"
BASE_URL = "https://ahunter.ru/site/suggest/address"
COUNT = 5
TIMEOUT = httpx.Timeout(4.0, connect=2.0)
USER_AGENT = "LeadChat/1.0 (address-check)"

#: Типы кусков строки Ahunter → к какому полю относится кусок. Тип стоит
#: впереди имени («г Ангарск», «кв-л 57»), у дома — «дом 8», «корп 2», «стр 1».
_РЕГИОН = frozenset({"обл", "край", "респ", "ао", "аобл", "окр", "чувашия"})
_РАЙОН = frozenset(
    {"р-н", "м.р-н", "г.о.", "у", "вн.тер.г.", "вн.р-н", "п.о.", "с.п.", "с/с", "г.п."}
)
_ГОРОД = frozenset({"г", "г."})
_ПУНКТ = frozenset(
    {
        "п",
        "пос",
        "пгт",
        "гп",
        "рп",
        "кп",
        "дп",
        "с",
        "д",
        "х",
        "ст-ца",
        "ст",
        "аул",
        "сл",
        "нп",
        "снт",
        "тер",
        "тер.",
        "днт",
        "днп",
        "тсн",
        "сп",
        "жт",
        "массив",
        "м",
        "промзона",
        "ж/д_ст",
        "ж/д",
        "рзд",
        "казарма",
        "гск",
        "г-к",
    }
)
#: Домовой кусок — тип и номер С ЦИФРЫ («дом 8», «корп 2», «стр 1»); литера —
#: одной буквой. «д Ольхово» — деревня, не дом.
#: «к.2» — без пробела, «дом 5 А» — литера через пробел, «лит АБ» — две буквы.
_ДОМ = re.compile(
    r"^(?:(?P<тип>дом|д|влд|двлд|владение|зд|соор|стр|строение|корп|корпус|к)\.?\s*"
    r"(?P<n>\d\S*(?:\s+[А-ЯЁа-яё](?![А-ЯЁа-яё]))?)"
    r"|(?P<тип_лит>лит|литера)\.?\s+(?P<lit>[А-ЯЁа-яё]{1,2}))$",
    re.IGNORECASE,
)
#: Полные слова домовых типов — к сокращениям справочника.
_ДОМ_КАНОН = {"корпус": "корп", "строение": "стр", "владение": "влд"}
_КУСОК = re.compile(r"^(?P<тип>[а-яё.\-/_]+)\s+(?P<имя>.+)$", re.IGNORECASE)


def parse_value(value: str) -> SuggestedAhunter | None:
    """«обл Иркутская, г Ангарск, кв-л 57, дом 8» → компоненты; без дома — None.

    Улица — всё, что не регион, район, город, пункт и не дом («кв-л 57»,
    «ул Ленина», «мкр 12» — с типом, как пишет справочник: карте так и надо).
    Дом собирается из всех домовых кусков: «дом 8, корп 2» → «8 корп 2».
    """
    регион = район = город = пункт = None
    улицы: list[str] = []
    дом: list[str] = []
    for кусок in value.split(","):
        кусок = кусок.strip()
        if not кусок:
            continue
        m_дом = _ДОМ.match(кусок)
        if m_дом is not None:
            тип = (m_дом.group("тип") or m_дом.group("тип_лит")).lower()
            тип = _ДОМ_КАНОН.get(тип, тип)
            n = (m_дом.group("n") or m_дом.group("lit") or "").strip()
            дом.append(n if тип in ("дом", "д") else f"{тип} {n}")
            continue
        m = _КУСОК.match(кусок)
        тип = m.group("тип").lower() if m else ""
        имя = m.group("имя").strip() if m else кусок
        if тип in _РЕГИОН:
            регион = кусок
        elif тип in _РАЙОН:
            район = кусок
        elif тип in _ГОРОД:
            # Второй город в строке («г Москва, г Зеленоград») — сам город
            # объявления, первый — город-регион: воркер сверяет город подсказки
            # с городом объявления, и Зеленоград обязан остаться городом.
            if город is not None:
                регион = регион or f"г {город}"
            город = имя
        elif тип in _ПУНКТ:
            пункт = имя if тип in ("п", "пос", "пгт", "рп", "с", "д", "х", "гп") else кусок
        else:
            улицы.append(кусок)
    if not дом:
        return None
    if not улицы and пункт is None and город is None:
        return None
    return SuggestedAhunter(
        street=" ".join(улицы),
        house=" ".join(дом),
        city=город,
        settlement=пункт,
        district=район,
        region=регион,
        formatted=value,
    )


def parse_response(данные: Any) -> list[SuggestedAhunter]:
    """Ответ Ahunter → подсказки с домом. Чистая функция — проверяется на образце."""
    if not isinstance(данные, dict):
        raise ProviderError("bad_response", None, "ответ не объект")
    подсказки = данные.get("suggestions")
    if подсказки is None:
        return []
    if not isinstance(подсказки, list):
        raise ProviderError("bad_response", None, "suggestions не список")
    out: list[SuggestedAhunter] = []
    for п in подсказки:
        if not isinstance(п, dict) or not isinstance(п.get("value"), str):
            continue
        разобрано = parse_value(п["value"])
        if разобрано is not None:
            out.append(разобрано)
    return out


def url_for(text: str) -> str:
    # Параметры Ahunter разделяются «;», а не «&» — собираем строку сами.
    return f"{BASE_URL}?output=json;count={COUNT};query={quote(text, safe='')}"


async def suggest(text: str) -> list[SuggestedAhunter]:
    """Один GET к Ahunter с готовой строкой запроса."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with http.client(TIMEOUT, headers) as own:
            resp = await own.get(url_for(text))
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    http.check_status(resp)
    return parse_response(http.parse_json(resp))
