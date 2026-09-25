"""Что LeadChat делает с ответом карты после шлюза.

Походы к OSM и Яндексу и разбор их ответов с 16.09 живут в шлюзе и проверяются
в `gateway/tests/test_gw_osm_yandex.py`; обёртки (кэш, темп, счётчики, маппинг
отказов) — в `test_osm_yandex_шлюз.py`. Здесь — только правила LeadChat про
уже разобранный `GeoHit`.
"""

from app.services import geocode as g


def test_тип_пункта_от_яндекса_не_дублируется_словом_клиента() -> None:
    """Яндекс отдаёт пункт с типом («посёлок Заречный») — строка адреса не
    должна получить «посёлок посёлок Заречный» из слова клиента."""
    hit = g.GeoHit(
        street="Звенигородская улица",
        house="1",
        settlement="посёлок Заречный",
        city="Орск",
        region="Оренбургская область",
        lat=51.210123,
        lon=58.501234,
        house_level=True,
        settlement_kind="district",
    )
    parsed = g.Parsed(
        street="ул звенигородская", house="1", settlement="заречный", settlement_type="посёлок"
    )
    assert g.format_address(hit, parsed) == "Звенигородская улица, 1, посёлок Заречный, Орск"
