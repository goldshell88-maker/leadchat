"""Рабочие часы — настройка, а не константа в двух местах сразу (#41).

ЧТО БЫЛО. «Скорость первого ответа в рабочие часы» считалась по интервалу
10:00–20:00, зашитому ДВАЖДЫ: в питоновском эталоне (app/services/stats.py) и
в умолчаниях SQL-функции `business_seconds_between`. Поменять их можно было
только правкой файлов на сервере с перезапуском — то есть через инженера, хотя
решение это управленческое.

ПОЧЕМУ ИМЕННО СЕЙЧАС. Часы участвуют в определении материализованного
представления статистики. Пока база маленькая, перестройка витрины ничего не
стоит; после загрузки годовой истории с девяти каналов та же правка станет
долгой операцией на боевом объёме. Задача и называется «пока база маленькая».

И ОНИ ВРУТ ПО СУЩЕСТВУ. У заказчика двенадцатичасовые смены, а окно 10–20
десятичасовое: клиенту, написавшему в 20:05 и отвеченному в 10:05 следующего
утра, засчитывается пять минут вместо четырнадцати часов. Цифра, которой
меряют работу, систематически льстит ночной смене. Само число здесь не
меняется — это решение владельца, — но теперь его можно поменять из интерфейса
за секунду, а не за выкатку.

КАК СДЕЛАНО, И ПОЧЕМУ НЕ ИНАЧЕ
-------------------------------
Первым заходом я убрал у функции умолчания-константы и завёл отдельную
двухаргументную версию, читающую настройку. Это не прошло, и хорошо, что не
прошло: `DROP FUNCTION` упёрся в зависимость — от четырёхаргументной функции
зависит сама витрина. Оставить обе версии тоже нельзя: вызов с двумя
аргументами стал бы неоднозначным.

Работает третий вариант, и он же самый скромный. Умолчание параметра — это
выражение, и оно вычисляется при КАЖДОМ вызове. Значит достаточно поставить в
умолчание обращение к маленькой функции, которая читает настройку:

    work_start interval DEFAULT stats_work_hour('stats.work_start_hour', 10)

Сигнатура не меняется, поэтому `CREATE OR REPLACE` проходит без единого
`DROP`: ни функцию, ни витрину ронять не нужно, определение представления
остаётся прежним байт в байт. Смена часов не требует ни миграции, ни
пересборки — ближайший ежечасный `REFRESH` пересчитает всё по новым значениям.

Умолчания на случай пустой таблицы настроек остаются прежними (10 и 20):
миграция не должна менять цифры в чужих отчётах молча. Меняет их человек,
осознанно, и видя результат.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Час из таблицы настроек в виде интервала.
#:
#: `STABLE`, а не `IMMUTABLE`: значение зависит от содержимого таблицы. Соври
#: планировщику об этом — и он вправе вычислить функцию один раз и подставить
#: результат туда, где настройку только что поменяли.
#:
#: НЕ `STRICT`: запасное значение обязано сработать именно тогда, когда строки
#: настройки нет вовсе.
WORK_HOUR_FN = """
CREATE OR REPLACE FUNCTION stats_work_hour(setting_key text, fallback int)
RETURNS interval
LANGUAGE sql STABLE AS $$
    SELECT make_interval(hours => COALESCE(
        (SELECT (value #>> '{}')::int FROM app_settings WHERE key = setting_key),
        fallback))
$$
"""

#: Та же арифметика, что и была, — меняются только умолчания параметров.
BUSINESS_SECONDS_FN = """
CREATE OR REPLACE FUNCTION business_seconds_between(
    t0 timestamptz,
    t1 timestamptz,
    work_start interval DEFAULT stats_work_hour('stats.work_start_hour', 10),
    work_end   interval DEFAULT stats_work_hour('stats.work_end_hour', 20)
) RETURNS bigint
LANGUAGE sql STABLE STRICT AS $$
    SELECT COALESCE(sum(GREATEST(0, EXTRACT(epoch FROM
               LEAST   (t1 AT TIME ZONE 'Europe/Moscow', d + work_end)
             - GREATEST(t0 AT TIME ZONE 'Europe/Moscow', d + work_start)
           )))::bigint, 0)
    FROM generate_series(
           date_trunc('day', t0 AT TIME ZONE 'Europe/Moscow'),
           date_trunc('day', t1 AT TIME ZONE 'Europe/Moscow'),
           interval '1 day') AS d
$$
"""

#: Прежние умолчания-константы — для отката.
WITH_CONSTANTS = """
CREATE OR REPLACE FUNCTION business_seconds_between(
    t0 timestamptz,
    t1 timestamptz,
    work_start interval DEFAULT interval '10 hours',
    work_end   interval DEFAULT interval '20 hours'
) RETURNS bigint
LANGUAGE sql STABLE STRICT AS $$
    SELECT COALESCE(sum(GREATEST(0, EXTRACT(epoch FROM
               LEAST   (t1 AT TIME ZONE 'Europe/Moscow', d + work_end)
             - GREATEST(t0 AT TIME ZONE 'Europe/Moscow', d + work_start)
           )))::bigint, 0)
    FROM generate_series(
           date_trunc('day', t0 AT TIME ZONE 'Europe/Moscow'),
           date_trunc('day', t1 AT TIME ZONE 'Europe/Moscow'),
           interval '1 day') AS d
$$
"""


def upgrade() -> None:
    op.execute(WORK_HOUR_FN)
    op.execute(BUSINESS_SECONDS_FN)


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.execute(WITH_CONSTANTS)
    op.execute("DROP FUNCTION IF EXISTS stats_work_hour(text, int)")
