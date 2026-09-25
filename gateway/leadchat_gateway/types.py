"""Общие типы ручек шлюза.

Имена полей — один в один с dataclass-ами LeadChat (`app/services/geocode.py`:
GeoHit, PlaceHit; `app/integrations/yandex_suggest.py` и `ahunter.py`:
Suggested): обёртка на той стороне собирает их `GeoHit(**d)` и не должна ничего
переименовывать. Добавлять поля можно, убирать и переименовывать — нет.
"""

from __future__ import annotations

from pydantic import BaseModel


class Query(BaseModel):
    """Что LeadChat спрашивает у карты. Собирается ТОЛЬКО из компонентов
    разбора адреса — никогда из текста сообщения (там телефон, имя, код
    домофона). `street_for_map`/`free_text` LeadChat считает своим словарём
    типов улиц; шлюз словаря не знает и строки не пересобирает."""

    region: str | None = None
    city: str | None = None
    settlement: str | None = None
    street: str = ""
    house: str = ""
    street_for_map: str = ""
    free_text: str = ""


class GeoHit(BaseModel):
    street: str | None
    house: str | None
    settlement: str | None
    city: str | None
    region: str | None
    lat: float
    lon: float
    house_level: bool
    interpolated: bool = False
    settlement_kind: str = "place"
    #: Точка — самого дома (или ближайшего), а не улицы/пункта (18.09: DaData
    #: отдаёт дом ФИАС с `qc_geo` 2/3 — дом есть, точка врёт до полутора
    #: километров). Дом в справочнике при этом остаётся домом.
    precise: bool = True
    #: Планировочная структура, в которой стоит дом: массив, СНТ, ДНТ, КП,
    #: микрорайон — как пишет карта («СНТ Заречное», «мкр Центральный»).
    area: str | None = None


class PlaceHit(BaseModel):
    name: str
    kind: str
    settlement: str | None
    area: str | None
    city: str | None
    district: str | None
    region: str | None
    lat: float
    lon: float


class SuggestedYandex(BaseModel):
    street: str
    house: str
    city: str | None
    settlement: str | None
    region: str | None
    formatted: str | None


class SuggestedAhunter(BaseModel):
    street: str
    house: str
    city: str | None
    settlement: str | None
    district: str | None
    region: str | None
    formatted: str
