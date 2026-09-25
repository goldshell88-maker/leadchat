"""Круглосуточные рабочие часы можно ВЫСТАВИТЬ (правка владельца 22.08).

ЧТО БЫЛО. Окно первого ответа задаётся парой часов, и сервер требует
`начало < конец` (`app_settings.work_window_is_set`). Часы валидируются
диапазоном 0..23 каждый по отдельности. Значит максимум, что удавалось
выставить, — 0:00–23:00, то есть **двадцать три часа**: последний час суток
из расчёта выпадал всегда.

Круглосуточно выставить было нельзя вовсе:
  * 0–0 и 23–0 сервер отвергает как вырожденное окно (и правильно: на боевой
    системе такое окно уже давало ноль рабочих секунд у ВСЕХ менеджеров);
  * запасная ветка «окно не задано → считаем календарное время» есть и в
    Python, и в SQL, но добраться до неё через интерфейс невозможно — её
    закрывает та самая проверка.

ЧТО СТАЛО. Конец окна принимает 24 — «до конца суток». Пара 0–24 проходит
проверку непустого окна (0 < 24), а арифметика уже умеет: `day + 24 часа` —
это полночь следующего дня и в Python, и в SQL (`make_interval` час не
обрезает, миграция не нужна).

24 разрешено ТОЛЬКО концу окна. «С 24:00» не значит ничего, и разрешить это
началу — значит завести пару 24–24, которая снова вырожденная.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import ApiError
from app.services import app_settings, stats

МСК = 3  # смещение Москвы; время в расчёте приводится к нему


def _мск(день: int, час: int, минута: int = 0) -> datetime:
    """Момент по Москве, выраженный в UTC. Через timedelta, а не вычитанием
    из часа: 00:20 по Москве — это 21:20 ПРЕДЫДУЩИХ суток по UTC."""
    return datetime(2026, 8, день, 0, 0, tzinfo=UTC) + timedelta(hours=час - МСК, minutes=минута)


def test_konets_okna_prinimayet_24():
    spec = app_settings.SPECS[app_settings.STATS_WORK_END_HOUR]
    assert app_settings._validate_hour(spec, 24) == 24


def test_nachalo_okna_24_ne_prinimayet():
    """«С 24:00» не значит ничего, а пара 24–24 снова вырожденная."""
    spec = app_settings.SPECS[app_settings.STATS_WORK_START_HOUR]
    with pytest.raises(ApiError):
        app_settings._validate_hour(spec, 24)


def test_okno_0_24_schitayetsya_zadannym():
    assert app_settings.work_window_is_set(0, 24) is True


def test_posledniy_chas_sutok_bolshe_ne_teryayetsya():
    """Клиент написал в 23:10, ответили в 23:40 — тридцать минут, а не ноль."""
    t0, t1 = _мск(10, 23, 10), _мск(10, 23, 40)
    было = stats.business_seconds_between(
        t0, t1, work_start=timedelta(hours=0), work_end=timedelta(hours=23)
    )
    стало = stats.business_seconds_between(
        t0, t1, work_start=timedelta(hours=0), work_end=timedelta(hours=24)
    )
    assert было == 0, "проверка бессмысленна: старое окно этот час и так считало"
    assert стало == 30 * 60


def test_kruglosutochno_schitayet_vsyo_vklyuchaya_noch():
    """Написал в 23:50, ответили в 00:20 — полчаса через полночь."""
    t0, t1 = _мск(10, 23, 50), _мск(11, 0, 20)
    вышло = stats.business_seconds_between(
        t0, t1, work_start=timedelta(hours=0), work_end=timedelta(hours=24)
    )
    assert вышло == 30 * 60


def test_obychnoye_okno_ne_slomano():
    """Смена 10–20: ночь не считается, утренние минуты — считаются."""
    t0, t1 = _мск(10, 20, 5), _мск(11, 10, 5)
    вышло = stats.business_seconds_between(
        t0, t1, work_start=timedelta(hours=10), work_end=timedelta(hours=20)
    )
    assert вышло == 5 * 60, "окно 10–20 обязано дать утренние пять минут, а не ночь"


def test_kruglosutochno_pokazyvayet_nastoyashchee_ozhidanie():
    """То же ожидание при окне 0–24 — четырнадцать часов, а не пять минут.

    Это и есть смысл правки: на круглосуточной работе цифра перестаёт льстить
    ночной смене."""
    t0, t1 = _мск(10, 20, 5), _мск(11, 10, 5)
    вышло = stats.business_seconds_between(
        t0, t1, work_start=timedelta(hours=0), work_end=timedelta(hours=24)
    )
    assert вышло == 14 * 3600
