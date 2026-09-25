"""Рабочие часы задаются из интерфейса и нигде не разъезжаются (#41).

ЧТО БЫЛО. «Скорость первого ответа в рабочие часы» считалась по интервалу
10:00–20:00, зашитому ДВАЖДЫ: в питоновском эталоне и в умолчаниях SQL-функции.
Поменять их можно было только правкой файлов на сервере с перезапуском — то
есть через инженера, хотя решение управленческое: меняются смены, меняется
окно, по которому меряют скорость ответа.

И ЦИФРА ВРЁТ ПО СУЩЕСТВУ. У заказчика двенадцатичасовые смены, а окно
десятичасовое: клиенту, написавшему в 20:05 и отвеченному в 10:05 утра,
засчитывается пять минут вместо четырнадцати часов. Само число здесь не
меняется — менять чужие отчёты молча нельзя, — но теперь оно меняется из
интерфейса за секунду, а не за выкатку.

ЗДЕСЬ ЗАПЕРТЫ ТРИ МЕСТА, где умолчание живёт порознь и обязано совпадать:
объявление настройки, запасное значение внутри SQL-функции и константа
питоновского эталона. Расхождение любых двух не падает и не подсвечивается —
оно просто даёт другую цифру в отчёте.

И ЗАПЕРТО ВЫРОЖДЕННОЕ ОКНО — та беда, ради которой файл переписывался.
На боевой системе часы были сохранены как 0:00–0:00: интерфейс подсвечивал
это красным, но сохранить позволял, а сервер проверял только «час от 0 до 23»,
каждый конец отдельно. Окно нулевой длины даёт ноль рабочих секунд на любом
интервале, и медиана «первый ответ в рабочее время» стала нулём у ВСЕХ
менеджеров — отчёт, по которому оценивают тринадцать диспетчеров, показывал
ноль независимо от их работы и выглядел при этом совершенно обычно.

Отсюда две проверки, и они не заменяют друг друга: вход закрыт
(`_assert_work_window`), а уже сохранённое окно считается по календарю
(`business_seconds_between`) — потому что оно СОХРАНЕНО и врёт прямо сейчас,
до того как владелец откроет настройки.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.services import app_settings, stats

pytestmark = pytest.mark.anyio

VERSIONS = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations" / "versions"


def _active_migration() -> str:
    """Текст ПОСЛЕДНЕЙ миграции, переопределяющей `business_seconds_between`.

    Не имя файла жёстко: функцию переписывали уже дважды (0023 — читать
    настройку, 0028 — запасной вариант), и тест, прибитый к конкретной
    ревизии, после третьего раза сторожил бы мёртвый код.
    """
    files = sorted(
        p
        for p in VERSIONS.glob("*.py")
        if "CREATE OR REPLACE FUNCTION business_seconds_between" in p.read_text(encoding="utf-8")
    )
    assert files, "ни одна миграция не объявляет business_seconds_between"
    return files[-1].read_text(encoding="utf-8")


def _sql_fallbacks() -> tuple[int, int]:
    """Запасные часы из COALESCE внутри действующей SQL-функции."""
    source = _active_migration()
    body = source.split("BUSINESS_SECONDS_FN = ", 1)[1].split("\n#: ", 1)[0]
    hours = [int(n) for n in re.findall(r"stats_work_hour\('[^']+',\s*(\d+)\)", body)]
    assert len(hours) == 2, f"не разобрал запасные часы из миграции: {hours}"
    return hours[0], hours[1]


def test_the_setting_is_declared_for_both_ends() -> None:
    assert app_settings.STATS_WORK_START_HOUR in app_settings.SPECS
    assert app_settings.STATS_WORK_END_HOUR in app_settings.SPECS


def test_the_default_is_the_same_in_the_setting_and_in_sql() -> None:
    """Иначе пустая таблица настроек даст одну цифру, а объявление — другую."""
    start, end = _sql_fallbacks()
    assert app_settings.SPECS[app_settings.STATS_WORK_START_HOUR].default == start
    assert app_settings.SPECS[app_settings.STATS_WORK_END_HOUR].default == end


def test_the_python_reference_uses_the_same_default() -> None:
    """Эталон сверяется с SQL на одних данных — по разным часам сверять нечего."""
    assert stats.WORK_START.total_seconds() / 3600 == (
        app_settings.SPECS[app_settings.STATS_WORK_START_HOUR].default
    )
    assert stats.WORK_END.total_seconds() / 3600 == (
        app_settings.SPECS[app_settings.STATS_WORK_END_HOUR].default
    )


@pytest.mark.parametrize("bad", [-1, 24, 100, "10", True, None])
def test_nonsense_hours_are_refused(bad: object) -> None:
    """Час суток — от 0 до 23. Строка «10» и `True` тоже отвергаются: первое
    прошло бы в JSONB как есть, второе в Python неотличимо от единицы."""
    with pytest.raises(ApiError):
        app_settings._validate(app_settings.SPECS[app_settings.STATS_WORK_START_HOUR], bad)


@pytest.mark.parametrize("good", [0, 8, 10, 20, 23])
def test_sensible_hours_pass(good: int) -> None:
    spec = app_settings.SPECS[app_settings.STATS_WORK_START_HOUR]
    assert app_settings._validate(spec, good) == good


def test_one_end_alone_says_nothing_about_the_window() -> None:
    """`_validate` видит одно значение и сравнивать его не с чем — это норма.

    Проверять пару обязан `set_many`, и раньше он этого не делал: отсюда
    сохранённые 0:00–0:00. Строка ниже сторожит границу ответственности —
    поштучная проверка не должна начать «догадываться» о втором конце.
    """
    assert app_settings._validate(app_settings.SPECS[app_settings.STATS_WORK_START_HOUR], 22) == 22
    assert app_settings._validate(app_settings.SPECS[app_settings.STATS_WORK_END_HOUR], 8) == 8


# ------------------------------------------------- вырожденное окно не сохраняется


@pytest.mark.parametrize(
    "start,end",
    [
        (0, 0),  # ровно то, что стоит на боевой системе
        (10, 10),
        (20, 8),  # «ночная смена» — но окно не переходит через полночь, значит тот же ноль
    ],
)
async def test_a_degenerate_window_is_refused(db, start: int, end: int) -> None:
    with pytest.raises(ApiError) as err:
        await app_settings.set_many(
            db,
            {
                app_settings.STATS_WORK_START_HOUR: start,
                app_settings.STATS_WORK_END_HOUR: end,
            },
            user_id=None,
        )
    assert err.value.status == 400
    # И НИЧЕГО НЕ ЗАПИСАЛОСЬ: половина применённых изменений хуже отказа.
    saved = await app_settings.get_all(db)
    assert saved[app_settings.STATS_WORK_START_HOUR] == 10
    assert saved[app_settings.STATS_WORK_END_HOUR] == 20


async def test_a_sensible_window_still_saves(db) -> None:
    """Охранник от чрезмерного запрета: обычная правка окна обязана проходить."""
    saved = await app_settings.set_many(
        db,
        {app_settings.STATS_WORK_START_HOUR: 8, app_settings.STATS_WORK_END_HOUR: 22},
        user_id=None,
    )
    assert saved[app_settings.STATS_WORK_START_HOUR] == 8
    assert saved[app_settings.STATS_WORK_END_HOUR] == 22


async def test_changing_the_ends_one_by_one_is_not_blocked(db) -> None:
    """Старое объяснение «запрет не даст поменять 10–20 на 8–22» было неправдой.

    Сравнивается ИТОГОВАЯ пара, а не присланная: недосланный конец берётся из
    сохранённого. Поэтому путь по одному полю проходит целиком — а экран
    настроек и так шлёт оба поля разом.
    """
    await app_settings.set_many(db, {app_settings.STATS_WORK_START_HOUR: 8}, user_id=None)
    saved = await app_settings.set_many(db, {app_settings.STATS_WORK_END_HOUR: 22}, user_id=None)
    assert (saved[app_settings.STATS_WORK_START_HOUR], saved[app_settings.STATS_WORK_END_HOUR]) == (
        8,
        22,
    )


async def test_a_neighbouring_setting_is_untouched_by_the_window_check(db) -> None:
    """Проверка окна не должна цепляться к настройкам, где часов нет вовсе."""
    saved = await app_settings.set_many(db, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
    assert saved[app_settings.DISTRIBUTION_ENABLED] is True


# --------------------------------------- запасной расчёт: окно не задано → календарь


def _msk(hour: int, minute: int = 0) -> datetime:
    """Момент по московским стенным часам — окно задано именно в них (06 §0.1)."""
    return datetime(2026, 8, 10, hour, minute, tzinfo=stats.MSK).astimezone(UTC)


@pytest.mark.parametrize(
    "start,end",
    [(0, 0), (10, 10), (20, 8)],
)
def test_an_unset_window_falls_back_to_calendar_time(start: int, end: int) -> None:
    """Главная правка: ноль был бы враньём «ответили мгновенно».

    Диалог, на который ответили через три часа, обязан дать три часа, а не
    ноль, — иначе колонка отчёта показывает ноль независимо от работы.
    """
    t0 = _msk(9)
    t1 = _msk(12)
    value = stats.business_seconds_between(t0, t1, timedelta(hours=start), timedelta(hours=end))
    assert value == 3 * 3600


def test_the_fallback_keeps_the_edge_cases() -> None:
    """Краевые случаи прежние: NULL → None, перевёрнутый интервал → 0."""
    zero = (timedelta(0), timedelta(0))
    assert stats.business_seconds_between(None, _msk(12), *zero) is None
    assert stats.business_seconds_between(_msk(12), None, *zero) is None
    assert stats.business_seconds_between(_msk(12), _msk(9), *zero) == 0


def test_a_real_window_still_clips_the_night() -> None:
    """Охранник от чрезмерной правки: заданное окно обязано считать по-старому.

    Без этой строки «всегда календарь» выглядел бы такой же зелёной правкой,
    как запасной вариант, — а разница между ними в том, меряем мы рабочие часы
    или вообще ничего не меряем.
    """
    value = stats.business_seconds_between(
        _msk(8), _msk(21), timedelta(hours=10), timedelta(hours=20)
    )
    assert value == 10 * 3600


def test_the_sql_function_has_the_same_fallback() -> None:
    """Сторож разъезда: считает отчёт SQL, а тесты выше — Python.

    Совпадение цифра в цифру проверяет интеграционный тест на настоящем
    PostgreSQL; здесь — что ветка вообще не потерялась при следующей правке
    функции (её переписывали `CREATE OR REPLACE`, то есть целиком).
    """
    body = _active_migration().split("BUSINESS_SECONDS_FN = ", 1)[1].split("\n#: ", 1)[0]
    assert "work_end <= work_start" in body, "в SQL-функции нет ветки «окно не задано»"
    assert "EXTRACT(epoch FROM t1 - t0)" in body, "запасной вариант считает не календарь"


def test_the_window_predicate_is_shared() -> None:
    """Одно определение «окно задано» на форму и на расчёт.

    Разъедься они — появилось бы окно, которое сохранить нельзя, но которое
    метрики считают нормальным, или наоборот.
    """
    assert app_settings.work_window_is_set(10, 20) is True
    assert app_settings.work_window_is_set(0, 0) is False
    assert app_settings.work_window_is_set(20, 8) is False
