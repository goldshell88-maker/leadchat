"""Ручки настроек распределения (`/settings/distribution`).

Главное здесь — не «сохраняется ли», а два свойства, которые ломаются тише
всего: что «снять ограничение» отличимо от «не трогали», и что изменение
настройки, влияющей на работу всей смены, не проходит анонимно.
"""

import pytest

from app.models import AuditLog
from app.services import app_settings

pytestmark = pytest.mark.anyio

URL = "/api/v1/settings/distribution"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_only_admin_can_read_or_change(client, tokens):
    """Потолок диалогов на человека — норма выработки; оператору знать её
    незачем, а руководителю без `settings:manage` — менять."""
    for role in ("head", "manager", "observer"):
        assert (await client.get(URL, headers=hdr(tokens[role]))).status_code == 403
        assert (
            await client.patch(URL, json={"enabled": True}, headers=hdr(tokens[role]))
        ).status_code == 403


async def test_defaults_are_shown_before_anything_is_saved(client, tokens):
    res = await client.get(URL, headers=hdr(tokens["admin"]))
    assert res.status_code == 200
    # Ровно то поведение, что было до появления настроек: диалоги берут руками.
    assert res.json() == {
        "enabled": False,
        "max_active": 5,
        "release_unavailable": True,
        "release_exempt_ids": [],
    }


async def test_unlimited_is_distinguishable_from_not_touched(client, tokens):
    """«Без ограничения» и «поле не прислали» в JSON выглядят одинаково — оба
    `null`. Поэтому у снятия потолка отдельный признак; без него человек не смог
    бы снять ограничение вообще, а мог бы только поставить побольше."""
    headers = hdr(tokens["admin"])

    # Прислали только «включить» — потолок остаётся прежним.
    res = await client.patch(URL, json={"enabled": True}, headers=headers)
    assert res.json() == {
        "enabled": True,
        "max_active": 5,
        "release_unavailable": True,
        "release_exempt_ids": [],
    }

    # А теперь явно снимаем ограничение.
    res = await client.patch(URL, json={"max_active_unlimited": True}, headers=headers)
    assert res.json() == {
        "enabled": True,
        "max_active": None,
        "release_unavailable": True,
        "release_exempt_ids": [],
    }


async def test_out_of_range_cap_is_refused(client, tokens):
    """Верхняя граница — защита от опечатки: «500» вместо «5» означало бы
    раздачу без ограничения ровно там, где его хотели поставить."""
    res = await client.patch(URL, json={"max_active": 9999}, headers=hdr(tokens["admin"]))
    # 400, а не 422: у проекта единый конверт ошибок (01 §1.3), и ответы
    # валидации приводятся к нему же.
    assert res.status_code == 400


async def test_the_change_is_never_anonymous(client, tokens, users_by_role, db_sessionmaker):
    """«Почему со вчера диалоги не раздаются» обязан иметь ответ с именем."""
    await client.patch(URL, json={"enabled": True}, headers=hdr(tokens["admin"]))

    async with db_sessionmaker() as s:
        import sqlalchemy as sa

        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.distribution_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == users_by_role["admin"].id
    # И «было», и «стало»: без первого запись не отвечает на вопрос «что
    # изменилось», а только «кто-то что-то трогал».
    assert rows[0].details["before"]["enabled"] is False
    assert rows[0].details["after"]["enabled"] is True


async def test_a_no_op_patch_writes_nothing_to_the_journal(client, tokens, db_sessionmaker):
    """Повторное сохранение тех же значений журнал не засоряет.

    Иначе открытый экран настроек с автосохранением за смену насыпал бы сотню
    строк «изменил», среди которых настоящее изменение не найти.
    """
    await client.patch(URL, json={"enabled": False}, headers=hdr(tokens["admin"]))

    async with db_sessionmaker() as s:
        import sqlalchemy as sa

        count = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "settings.distribution_changed")
            )
        ).scalar_one()
    assert count == 0


async def test_the_setting_survives_a_restart(client, tokens):
    """Записанное читается обратно — то есть живёт в базе, а не в памяти
    процесса, как было с переменными окружения."""
    headers = hdr(tokens["admin"])
    await client.patch(URL, json={"enabled": True, "max_active": 7}, headers=headers)

    res = await client.get(URL, headers=headers)
    assert res.json() == {
        "enabled": True,
        "max_active": 7,
        "release_unavailable": True,
        "release_exempt_ids": [],
    }
    assert app_settings.MAX_ACTIVE_LIMIT == 100


# --- рабочие часы (#41) -------------------------------------------------------


async def test_work_hours_are_changed_from_the_interface(client, tokens):
    """Часы меняются запросом, а не правкой файла с перезапуском.

    В этом и была задача: окно, по которому считается «скорость первого ответа
    в рабочие часы», было зашито в код дважды, и поменять его мог только
    инженер — хотя решение управленческое.
    """
    r = await client.patch(
        "/api/v1/settings/work-hours",
        json={"start_hour": 8, "end_hour": 22},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"start_hour": 8, "end_hour": 22}

    r = await client.get(
        "/api/v1/settings/work-hours",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.json() == {"start_hour": 8, "end_hour": 22}


async def test_a_nonsense_hour_is_refused(client, tokens):
    r = await client.patch(
        "/api/v1/settings/work-hours",
        json={"start_hour": 25},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    # 400, а не 422: у проекта единый конверт ошибок (01 §1.3), и ответы
    # валидации приводятся к нему же — как у потолка автораздачи выше.
    assert r.status_code == 400


async def test_a_zero_length_window_is_refused_by_the_handle(client, tokens):
    """0:00–0:00 больше не сохраняется — и именно этот запрос приходит с экрана.

    Так и было сохранено на боевой системе: интерфейс подсвечивал пару
    красным, но кнопка «Сохранить часы» работала, а сервер возражений не имел.
    Дальше окно нулевой длины обнулило медиану «первый ответ в рабочее время»
    у всех менеджеров сразу — по этой колонке оценивают тринадцать человек.

    Проверяется ручка, а не только сервис: экран шлёт оба поля одним PATCH'ем,
    и отказ обязан прийти именно на него.
    """
    headers = {"Authorization": f"Bearer {tokens['admin']}"}
    r = await client.patch(
        "/api/v1/settings/work-hours", json={"start_hour": 0, "end_hour": 0}, headers=headers
    )
    assert r.status_code == 400, r.text

    # Прежние часы на месте: отказ ничего не переписал наполовину.
    r = await client.get("/api/v1/settings/work-hours", headers=headers)
    assert r.json() == {"start_hour": 10, "end_hour": 20}


async def test_only_the_owner_may_change_the_hours(client, tokens):
    """Цифры отчёта после этой правки читаются иначе, а выглядят так же.

    Поэтому право то же, что у автораздачи: и то и другое решает, как работает
    смена, — руководителю и оператору такого не дают.
    """
    for role in ("head", "manager", "observer"):
        r = await client.patch(
            "/api/v1/settings/work-hours",
            json={"start_hour": 8},
            headers={"Authorization": f"Bearer {tokens[role]}"},
        )
        assert r.status_code == 403, f"{role}: {r.text}"


# --- распознавание телефонов в тексте (правка 10 от 12 августа) ---------------

PHONE_URL = "/api/v1/settings/phone-detect"


async def test_phone_detect_defaults_are_recognize_but_only_suggest(client, tokens):
    """Умолчания — часть требования владельца, а не деталь реализации.

    «Распознавать» включено: иначе правка не работает ни у кого, пока
    руководитель не найдёт переключатель. «Писать сразу» выключено: тихая
    запись ЧУЖОГО номера в карточку хуже, чем несделанная работа — по номеру
    оттуда звонят и диктуют его мастеру вслух.
    """
    r = await client.get(PHONE_URL, headers=hdr(tokens["admin"]))
    assert r.status_code == 200, r.text
    # Наши номера пусты, автообъединение в коде ВЫКЛЮЧЕНО (12.09): включает
    # владелец на экране, а не выкатка.
    assert r.json() == {
        "enabled": True,
        "autofill": False,
        "own_numbers": "",
        "own_numbers_parsed": [],
        "merge_auto": "off",
    }


async def test_phone_detect_switches_survive_a_restart(client, tokens):
    """Записанное читается обратно — то есть живёт в базе, а не в памяти
    процесса: переключатель обязан пережить перезапуск сервиса."""
    headers = hdr(tokens["admin"])
    r = await client.patch(PHONE_URL, json={"autofill": True}, headers=headers)
    assert r.status_code == 200, r.text
    # Прислали одно поле — остальные остались прежними.
    assert r.json()["autofill"] is True and r.json()["enabled"] is True

    r = await client.get(PHONE_URL, headers=headers)
    assert (r.json()["enabled"], r.json()["autofill"]) == (True, True)


async def test_turning_recognition_off_does_not_touch_the_second_switch(client, tokens):
    """Выключатели независимы: «не разбирать» не отменяет решения о записи.

    Иначе включённая обратно правка молча приезжала бы с чужой настройкой
    автозаполнения — самой опасной из двух.
    """
    headers = hdr(tokens["admin"])
    await client.patch(PHONE_URL, json={"autofill": True}, headers=headers)
    r = await client.patch(PHONE_URL, json={"enabled": False}, headers=headers)
    assert (r.json()["enabled"], r.json()["autofill"]) == (False, True)


async def test_the_phone_detect_change_is_never_anonymous(
    client, tokens, users_by_role, db_sessionmaker
):
    """«С какого числа в карточках взялись эти номера» — вопрос с ответом.

    Включённая запись «сразу в карточку» означает, что цепочка цифр из чужого
    сообщения молча становится телефоном, по которому звонят. Такое не
    случается анонимно.
    """
    await client.patch(PHONE_URL, json={"autofill": True}, headers=hdr(tokens["admin"]))

    async with db_sessionmaker() as s:
        import sqlalchemy as sa

        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.phone_detect_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == users_by_role["admin"].id
    assert rows[0].details["before"]["autofill"] is False
    assert rows[0].details["after"]["autofill"] is True


async def test_a_no_op_phone_detect_patch_writes_nothing(client, tokens, db_sessionmaker):
    """Сохранение тех же значений журнал не засоряет — как и у автораздачи."""
    await client.patch(
        PHONE_URL, json={"enabled": True, "autofill": False}, headers=hdr(tokens["admin"])
    )

    async with db_sessionmaker() as s:
        import sqlalchemy as sa

        count = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "settings.phone_detect_changed")
            )
        ).scalar_one()
    assert count == 0


async def test_a_non_boolean_switch_is_refused(client, tokens):
    r = await client.patch(PHONE_URL, json={"enabled": 17}, headers=hdr(tokens["admin"]))
    # 400, а не 422: у проекта единый конверт ошибок (01 §1.3) — как у потолка
    # автораздачи и у часа статистики выше.
    assert r.status_code == 400, r.text


async def test_only_the_owner_may_touch_recognition(client, tokens):
    """Разбор трогает ЧУЖИЕ данные — мы решаем за клиента, что вот эти цифры
    его телефон. Право то же, что у автораздачи и рабочих часов."""
    for role in ("head", "manager", "observer"):
        assert (await client.get(PHONE_URL, headers=hdr(tokens[role]))).status_code == 403
        r = await client.patch(PHONE_URL, json={"enabled": False}, headers=hdr(tokens[role]))
        assert r.status_code == 403, f"{role}: {r.text}"


# --- служебные записи Авито в ленте (14 августа) ------------------------------
#
# ⚠ РУЧКИ НЕ БЫЛО, ХОТЯ НАСТРОЙКУ УЖЕ СЛУШАЛИ. Настройку завели 14 августа по
# дословной просьбе владельца («сделай так, чтобы можно было отключить все
# системные сообщения в диалоге, которые отправляет Авито, так как в самой Jivo
# их нет»), лента её честно спрашивает — а записать значение было нечем. То
# есть просьба оказалась выполнена наполовину, и заметить это можно было только
# придя на экран за выключателем, которого нет. Эти тесты и есть заслон от
# повторения: настройка без ручки записи — не настройка.

THREAD_URL = "/api/v1/settings/thread"


async def test_avito_system_records_are_shown_until_someone_hides_them(client, tokens):
    """Умолчание — ПОКАЗЫВАТЬ, и это часть требования, а не деталь.

    Настройка меняет состав ленты у всех тринадцати диспетчеров разом. Приедь
    она выкаткой во включённом виде — у людей молча пропала бы часть переписки,
    и вопрос «куда делись сообщения» они задали бы не настройке, а системе.
    """
    r = await client.get(THREAD_URL, headers=hdr(tokens["admin"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"avito_system_hidden": False}


async def test_the_switch_survives_a_restart(client, tokens):
    """Записанное читается обратно — значит живёт в базе, а не в памяти процесса."""
    headers = hdr(tokens["admin"])
    r = await client.patch(THREAD_URL, json={"avito_system_hidden": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"avito_system_hidden": True}

    assert (await client.get(THREAD_URL, headers=headers)).json() == {"avito_system_hidden": True}

    # И обратно: выключатель обязан ВОЗВРАЩАТЬ записи, а не прятать навсегда.
    # Он про показ, а не про удаление — за частью этих записей стоит действие
    # клиента («пользователь создал чат»).
    r = await client.patch(THREAD_URL, json={"avito_system_hidden": False}, headers=headers)
    assert r.json() == {"avito_system_hidden": False}


async def test_an_empty_patch_changes_nothing_and_writes_no_audit(client, tokens, db_sessionmaker):
    """Пустое тело — не «выключить», а «не трогали»."""
    import sqlalchemy as sa

    headers = hdr(tokens["admin"])
    await client.patch(THREAD_URL, json={"avito_system_hidden": True}, headers=headers)

    r = await client.patch(THREAD_URL, json={}, headers=headers)
    assert r.json() == {"avito_system_hidden": True}, "пустое тело не должно ничего менять"

    async with db_sessionmaker() as s:
        count = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "settings.thread_changed")
            )
        ).scalar_one()
    assert count == 1, "в журнал попала только настоящая смена, а не пустой PATCH"


async def test_hiding_the_records_is_written_down_with_a_name(client, tokens, db_sessionmaker):
    """Настройка меняет состав ленты у ВСЕХ — вопрос «куда делись служебные
    записи» обязан иметь ответ с именем и временем. Без журнала он превращается
    в подозрение, что переписка теряется."""
    import sqlalchemy as sa

    await client.patch(THREAD_URL, json={"avito_system_hidden": True}, headers=hdr(tokens["admin"]))
    async with db_sessionmaker() as s:
        row = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.thread_changed")
                )
            )
            .scalars()
            .one()
        )
    assert row.user_id is not None, "решение человека, а не системы"
    assert row.details["before"] == {"avito_system_hidden": False}
    assert row.details["after"] == {"avito_system_hidden": True}


async def test_only_the_owner_may_hide_the_records(client, tokens):
    """Право то же, что у соседних настроек: лента общая на всю компанию."""
    for role in ("head", "manager", "observer"):
        assert (await client.get(THREAD_URL, headers=hdr(tokens[role]))).status_code == 403
        r = await client.patch(
            THREAD_URL, json={"avito_system_hidden": True}, headers=hdr(tokens[role])
        )
        assert r.status_code == 403, f"{role}: {r.text}"


async def test_a_long_exemption_list_is_kept_whole(client, tokens):
    """56 исключений — все 56 (проверка 24.09).

    Строка списка обрезалась на 2000 знаках: 53-й идентификатор рвался и
    выпадал при разборе, а ответ был 200 и экран показывал 52 человека без
    признака потери — диалоги пропавших продолжали освобождаться.
    """
    import uuid

    wanted = sorted(str(uuid.uuid4()) for _ in range(56))
    res = await client.patch(URL, json={"release_exempt_ids": wanted}, headers=hdr(tokens["admin"]))
    assert res.status_code == 200, res.text
    assert res.json()["release_exempt_ids"] == wanted
    again = await client.get(URL, headers=hdr(tokens["admin"]))
    assert again.json()["release_exempt_ids"] == wanted


async def test_an_over_long_text_setting_is_refused_not_cut(db_sessionmaker):
    """Длиннее потолка — 400 со словами, а не молча урезанная строка."""
    from app.core.errors import ApiError

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as caught:
            await app_settings.set_many(
                s, {app_settings.PHONE_OWN_NUMBERS: "1" * 2001}, user_id=None
            )
    assert caught.value.status == 400
    assert "не больше 2000" in caught.value.message
