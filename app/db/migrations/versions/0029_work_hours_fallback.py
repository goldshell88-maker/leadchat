"""Окно рабочих часов не задано — считаем по календарю, а не нулём.

ЧТО БЫЛО, И ЧЕМ ЭТО ОБЕРНУЛОСЬ. На боевой системе рабочие часы сохранены как
0:00–0:00. Интерфейс подсвечивал это красным, но сохранить позволял, а сервер
проверял только «час от 0 до 23» — каждый конец отдельно, пару не проверял
никто. Дальше `business_seconds_between` честно делала то, что написано:
пересечение интервала с окном нулевой длины пусто на любом дне, значит ноль
секунд. Медиана «первый ответ в рабочее время» стала нулём у ВСЕХ менеджеров.

Это не редкая цифра в углу экрана. По этой колонке в «Отчёте по менеджерам»
оценивают тринадцать диспетчеров; она показывала ноль независимо от того, как
человек работал, и выглядела при этом совершенно обычно — не «нет данных», не
прочерк, а уверенный «0 с» напротив каждой фамилии.

ЧТО МЕНЯЕТСЯ. Ровно одна ветка в теле функции: если конец окна не позже
начала, окно считается НЕЗАДАННЫМ, и возвращается календарное время между
метками. Всё остальное — арифметика, сигнатура, `STABLE STRICT`, умолчания,
читающие настройку, — прежнее.

ПОЧЕМУ КАЛЕНДАРЬ, А НЕ НОЛЬ И НЕ NULL. Ноль означает «ответили мгновенно» —
это неправда, и отличить её от правды в отчёте нечем. NULL выбросил бы диалоги
из медианы, то есть заменил бы вранье пустотой (`percentile_cont` игнорирует
NULL), и колонка молча опустела бы. Календарь — единственный ответ, который
остаётся числом и остаётся правдой: «часы не заданы, поэтому меряем всё
время». Побочно он же и заметен: колонка совпадает с обычным FRT, расхождение
исчезает, и настройку хочется поправить.

ПОЧЕМУ ЭТОГО МАЛО БЕЗ ПРОВЕРКИ ФОРМЫ, И ПОЧЕМУ ПРОВЕРКИ МАЛО БЕЗ ЭТОГО.
Проверка (`app_settings._assert_work_window`) закрывает вход: сохранить
вырожденное окно больше нельзя. Но она не чинит уже сохранённое — а оно
сохранено, и отчёт врёт СЕЙЧАС, до того как владелец откроет настройки.
Поэтому запасной вариант живёт в расчёте.

ПОЧЕМУ `CREATE OR REPLACE` И БЕЗ ПЕРЕСБОРКИ ВИТРИНЫ. Сигнатура не меняется —
значит от функции по-прежнему зависит `mv_conversation_stats`, и ронять
ничего не нужно (ровно тот же приём, что в 0023). `REFRESH` здесь НЕ
запускается намеренно: на боевом объёме это минуты под блокировкой внутри
транзакции миграции, а ежечасное задание `refresh_stats_mv` пересчитает
витрину само. Цифры исправятся в течение часа после выкатки.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Умолчания — те же, что в 0023: читают настройку, запасное значение 10 и 20.
#: Дублируются здесь целиком, потому что `CREATE OR REPLACE` переписывает
#: объявление функции целиком, а не его часть. Совпадение с объявлением
#: настройки сторожит `tests/unit/test_work_hours_setting.py`.
BUSINESS_SECONDS_FN = """
CREATE OR REPLACE FUNCTION business_seconds_between(
    t0 timestamptz,
    t1 timestamptz,
    work_start interval DEFAULT stats_work_hour('stats.work_start_hour', 10),
    work_end   interval DEFAULT stats_work_hour('stats.work_end_hour', 20)
) RETURNS bigint
LANGUAGE sql STABLE STRICT AS $$
    SELECT CASE WHEN work_end <= work_start
        -- Окно не задано: меряем календарное время. GREATEST(0, ...)
        -- сохраняет прежнее поведение перевёрнутого интервала (t1 < t0 → 0).
        THEN GREATEST(0, EXTRACT(epoch FROM t1 - t0))::bigint
        ELSE (
            SELECT COALESCE(sum(GREATEST(0, EXTRACT(epoch FROM
                       LEAST   (t1 AT TIME ZONE 'Europe/Moscow', d + work_end)
                     - GREATEST(t0 AT TIME ZONE 'Europe/Moscow', d + work_start)
                   )))::bigint, 0)
            FROM generate_series(
                   date_trunc('day', t0 AT TIME ZONE 'Europe/Moscow'),
                   date_trunc('day', t1 AT TIME ZONE 'Europe/Moscow'),
                   interval '1 day') AS d
        )
    END
$$
"""

#: Версия 0023 — без запасного варианта, для отката.
WITHOUT_FALLBACK = """
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


def upgrade() -> None:
    op.execute(BUSINESS_SECONDS_FN)


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.execute(WITHOUT_FALLBACK)
