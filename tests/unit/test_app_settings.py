"""Настройки, которыми управляет руководитель (`app_settings`).

Проверяется не «сохраняется ли значение» — это просто. Проверяется то, на чём
настройка подводит: что до первой записи система ведёт себя ровно как раньше,
что «без ограничения» отличается от «не меняли», что мусор в таблице не роняет
приём сообщений, и что выключение действует немедленно, а не после
перезапуска.
"""

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.models import AppSetting
from app.services import app_settings

pytestmark = pytest.mark.anyio


async def test_before_the_first_write_behaviour_is_unchanged(db_sessionmaker):
    """Пустая таблица означает поведение ровно как до миграции.

    У заказчика вся команда принимает диалоги руками, и день переезда не должен
    начинаться с того, что диалоги вдруг стали приходить сами.
    """
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False


async def test_unlimited_is_a_value_and_not_an_absence(db_sessionmaker):
    """`None` — это «без ограничения», а не «не задано».

    Разница не теоретическая: при ручном приёме потолок не нужен вовсе (в Jivo
    у заказчика лимитов нет), и человек должен уметь его снять, а не только
    поставить побольше.
    """
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_MAX_ACTIVE: None}, user_id=None)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_MAX_ACTIVE) is None
        # Строка есть — то есть это записанное решение, а не отсутствие записи.
        assert await s.get(AppSetting, app_settings.DISTRIBUTION_MAX_ACTIVE) is not None


async def test_a_bad_value_is_refused_before_anything_is_written(db_sessionmaker):
    """Пачка применяется целиком или никак.

    Половина применённых изменений хуже отказа: человек, включивший раздачу и
    получивший отказ на потолке, будет уверен, что не сработало ничего, — а
    раздача уже пошла.
    """
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await app_settings.set_many(
                s,
                {
                    app_settings.DISTRIBUTION_ENABLED: True,
                    app_settings.DISTRIBUTION_MAX_ACTIVE: 9999,
                },
                user_id=None,
            )
        assert err.value.status == 400
        await s.rollback()

    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False


async def test_garbage_in_the_table_does_not_break_the_pipeline(db_sessionmaker):
    """Мусор в значении не роняет приём сообщений.

    Настройку читает каждое входящее обращение. Исключение здесь означало бы,
    что правка строки руками (или откат формата) останавливает приём — цена
    несопоставима с пользой строгости. Возвращаемся к значению из окружения:
    оно заведомо корректно.
    """
    async with db_sessionmaker() as s:
        s.add(AppSetting(key=app_settings.DISTRIBUTION_ENABLED, value="да"))
        await s.commit()

    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False
        assert (await app_settings.get_all(s))[app_settings.DISTRIBUTION_ENABLED] is False


async def test_an_unknown_key_is_refused(db_sessionmaker):
    """Опечатка в имени настройки — отказ, а не молча записанная строка.

    Иначе «настройка не сохраняется» выяснилось бы через неделю, и искать
    пришлось бы в таблице, где лежат обе — правильная и с опечаткой.
    """
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await app_settings.set_many(s, {"distribution.enable": True}, user_id=None)
        assert err.value.status == 400


async def test_who_changed_it_is_recorded(db_sessionmaker, make_user):
    """Кто менял — записано.

    «Почему со вчера диалоги не раздаются» — вопрос, у которого обязан быть
    ответ с именем.
    """
    admin = await make_user("boss@leadchat.test", role="admin")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=admin.id)
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(AppSetting, app_settings.DISTRIBUTION_ENABLED)
        assert row is not None
        assert row.updated_by_id == admin.id


async def test_switching_off_takes_effect_immediately(db_sessionmaker):
    """Выключение действует на следующем же чтении, без перезапуска.

    Значение читается на каждом обращении и не кэшируется намеренно: кэш даже
    на пять секунд означал бы, что после нажатия «выключить» диалоги ещё
    продолжают уезжать людям.
    """
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is True
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: False}, user_id=None)
        await s.commit()
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False


async def test_resetting_returns_to_what_the_engineer_configured(db_sessionmaker):
    """Удаление строки возвращает к значению из окружения.

    Ради этого свойства миграция и не делает `INSERT` со значениями по
    умолчанию: будь строки вставлены заранее, «сброс» стал бы невозможен.
    """
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
        await s.commit()

    async with db_sessionmaker() as s:
        await s.execute(
            sa.delete(AppSetting).where(AppSetting.key == app_settings.DISTRIBUTION_ENABLED)
        )
        await s.commit()

    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False


# ------------------------------------------------- одно чтение на проход (29.08)


def _count_settings_reads(engine) -> list[str]:  # noqa: ANN001
    """Собирает обращения к `app_settings`, доехавшие до драйвера."""
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if "app_settings" in statement:
            seen.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    return seen


async def test_one_pass_reads_the_table_once_for_every_reader(engine, db_sessionmaker):
    """Четыре слоя одного сообщения стоят одного обращения к базе, а не четырёх.

    Ровно за этим `one_pass` и заведён: на боевом идут десятки тысяч сообщений
    в сутки, и лишнее чтение здесь умножается на этот объём.
    """
    reads = _count_settings_reads(engine)
    async with db_sessionmaker() as s, app_settings.one_pass():
        assert await app_settings.get(s, app_settings.PHONE_DETECT_ENABLED) is True
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False
        assert await app_settings.get(s, app_settings.AVITO_SYSTEM_HIDDEN) is False
        await app_settings.get_all(s)
    assert len(reads) == 1, reads


async def test_without_a_pass_nothing_changes(engine, db_sessionmaker):
    """Вне прохода каждый читатель ходит в базу сам — как и раньше.

    Снимок не должен протекать в ручки и планировщик: там между чтениями
    проходит время, и старое значение было бы уже неправдой.
    """
    reads = _count_settings_reads(engine)
    async with db_sessionmaker() as s:
        await app_settings.get(s, app_settings.PHONE_DETECT_ENABLED)
        await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED)
    assert len(reads) == 2, reads


async def test_the_snapshot_does_not_outlive_the_pass(db_sessionmaker):
    """Следующее сообщение видит настройку, изменённую после предыдущего.

    Это и есть граница между снимком и кэшем: «выключил раздачу — она
    выключилась немедленно» держится на том, что снимка между сообщениями нет.
    """
    async with db_sessionmaker() as s, app_settings.one_pass():
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is False

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
        await s.commit()

    async with db_sessionmaker() as s, app_settings.one_pass():
        assert await app_settings.get(s, app_settings.DISTRIBUTION_ENABLED) is True


async def test_saving_inside_a_pass_returns_what_was_saved(db_sessionmaker):
    """Запись сбрасывает снимок: человек видит то, что только что сохранил.

    Проход открывает конвейер, а не ручка настроек, — но снимок, переживший
    запись, показал бы «сохранено» и старое значение рядом, и объяснить это
    человеку было бы нечем.
    """
    async with db_sessionmaker() as s, app_settings.one_pass():
        assert await app_settings.get(s, app_settings.PHONE_DETECT_AUTOFILL) is False
        after = await app_settings.set_many(
            s, {app_settings.PHONE_DETECT_AUTOFILL: True}, user_id=None
        )
        assert after[app_settings.PHONE_DETECT_AUTOFILL] is True
        assert await app_settings.get(s, app_settings.PHONE_DETECT_AUTOFILL) is True
