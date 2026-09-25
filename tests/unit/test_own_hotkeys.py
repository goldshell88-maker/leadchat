"""Личные горячие клавиши (требование заказчика от 13 августа).

«Сделай так, чтобы можно было переназначать горячие клавиши».

ГЛАВНОЕ, ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ, — не сама запись, а два её свойства, которые легко
потерять и невозможно заметить:

  * хранятся ТОЛЬКО отличия. Сохрани мы всю таблицу — первое же нажатие «Сохранить»
    заморозило бы у человека все восемнадцать действий в сегодняшнем виде, и
    завтрашняя правка умолчаний (новое действие, исправленное сочетание) до него бы
    не доехала. Причём молча: у всех работает, у него нет;
  * соседи в `ui_settings` не затираются. За клавишами туда придут тема и плотность
    списка, и запись целиком снесла бы их — тоже молча.
"""

import pytest
import sqlalchemy as sa

from app.models import AuditLog, User


async def _put(client, token: str, hotkeys: dict):
    return await client.put(
        "/api/v1/auth/me/hotkeys",
        json={"hotkeys": hotkeys},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_hotkeys_are_saved_and_returned(client, tokens, db_sessionmaker, users_by_role):
    resp = await _put(client, tokens["manager"], {"claim": ["Mod+KeyY"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["hotkeys"] == {"claim": ["Mod+KeyY"]}

    # И приезжают при следующем входе — настройка живёт у человека, а не в браузере.
    заголовки = {"Authorization": f"Bearer {tokens['manager']}"}
    me = await client.get("/api/v1/auth/me", headers=заголовки)
    assert me.json()["hotkeys"] == {"claim": ["Mod+KeyY"]}

    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        assert row is not None and row.ui_settings == {"hotkeys": {"claim": ["Mod+KeyY"]}}


async def test_empty_body_returns_everything_to_defaults(
    client, tokens, db_sessionmaker, users_by_role
):
    await _put(client, tokens["manager"], {"claim": ["Mod+KeyY"]})
    resp = await _put(client, tokens["manager"], {})
    assert resp.json()["hotkeys"] == {}
    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        # Не пустой объект, а отсутствие записи: «ничего не переназначал» и «переназначал
        # в пустоту» — одно и то же, и хранить второе значит хранить мусор.
        assert row is not None and (row.ui_settings or {}).get("hotkeys") is None


async def test_only_differences_are_stored(client, tokens, db_sessionmaker, users_by_role):
    """⚠ ХРАНИМ ОТЛИЧИЯ, А НЕ СНИМОК ВСЕЙ ТАБЛИЦЫ.

    Иначе завтрашняя правка умолчаний не доедет до того, кто однажды что-то менял, —
    и разница будет видна только по жалобе «у меня не работает, а у всех работает».
    """
    await _put(client, tokens["manager"], {"claim": ["Mod+KeyY"]})
    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        сохранено = (row.ui_settings or {})["hotkeys"]
    assert list(сохранено) == ["claim"]


async def test_other_ui_settings_survive(client, tokens, db_sessionmaker, users_by_role):
    """Соседей по `ui_settings` не затираем — за клавишами туда придут тема и плотность."""
    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        row.ui_settings = {"theme": "dark"}
        await s.commit()

    await _put(client, tokens["manager"], {"close": ["Mod+KeyG"]})
    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        assert row is not None
        assert row.ui_settings == {"theme": "dark", "hotkeys": {"close": ["Mod+KeyG"]}}


async def test_change_is_journaled(client, tokens, db_sessionmaker, users_by_role):
    """Клавиши личные, и с виду их правка — поломка.

    Через месяц вопрос «почему у него Ctrl+D не закрывает диалог» разрешается только
    строкой журнала: по экрану видно, что всё на месте.
    """
    await _put(client, tokens["manager"], {"close": ["Mod+KeyG"]})
    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "user.hotkeys_changed")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # В журнал едут ИМЕНА действий, а не сами сочетания: вопрос всегда «что он трогал».
    assert rows[0].details["actions"] == ["close"]


async def test_everyone_may_change_their_own(client, tokens):
    """Прав не спрашиваем: человек правит СВОЁ рабочее место, как и свой пароль.

    Наблюдателю клавиши тоже нужны — он ходит по списку и открывает диалоги.
    """
    for role in ("admin", "head", "manager", "observer"):
        resp = await _put(client, tokens[role], {"listNext": ["Mod+KeyN"]})
        assert resp.status_code == 200, f"{role}: {resp.text}"


async def test_absurdly_large_map_is_refused(client, tokens):
    """Потолок обязателен: поле читается на КАЖДЫЙ вход вместе с профилем.

    Без него сюда однажды приедет мегабайт, и возить его туда-сюда будут все
    тринадцать человек каждое утро.
    """
    много = {f"action{i}": ["Mod+KeyA"] for i in range(200)}
    resp = await _put(client, tokens["manager"], много)
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize(
    "имя,тело",
    [
        ("длинное имя действия", {"a" * 100: ["Mod+KeyA"]}),
        ("длинное сочетание", {"thread.reply": ["X" * 100]}),
        ("десять сочетаний на действие", {"thread.reply": ["Mod+KeyA"] * 10}),
        ("мегабайт в теле", {f"{i}{'a' * 99}": ["X" * 100] * 50 for i in range(60)}),
    ],
)
async def test_потолок_считает_РАЗМЕР_а_не_только_число_действий(client, tokens, имя, тело):
    """⚠ ЗАЩИТА, О КОТОРОЙ НАПИСАНО В ДОКСТРОКЕ, НЕ РАБОТАЛА (28.08).

    Стоял один `max_length=64` на самом словаре, а в Pydantic это число ПАР, не
    байт: ключи, сами сочетания и длина списков не ограничивались ничем.
    Проверено на живой схеме — тело в пять мегабайт принималось и уезжало в
    JSONB, откуда читается на КАЖДЫЙ вход вместе с профилем.

    Сторож выше проверял ровно то же обещание, но только по количеству, и
    оставался зелёным. Здесь закрыты остальные три оси.
    """
    resp = await _put(client, tokens["manager"], тело)
    assert resp.status_code in (400, 422), f"{имя}: принято без возражений"


async def test_обычные_клавиши_потолком_не_задеты(client, tokens):
    """Границы взяты с запасом: реальная раскладка обязана проходить."""
    resp = await _put(
        client,
        tokens["manager"],
        {"thread.reply": ["Mod+Shift+KeyT", "Alt+Digit0"], "listNext": ["Mod+KeyN"]},
    )
    assert resp.status_code == 200, resp.text


async def test_выключенное_сочетание_переживает_вход(
    client, tokens, db_sessionmaker, users_by_role
):
    """Пустой список — это «выключено», и он обязан дожить до следующего входа.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «сделать возможность отдельно выключать комбинации
    клавиш».

    ⚠ ПОЧЕМУ ПУСТОЙ СПИСОК, А НЕ НОВОЕ ПОЛЕ. Разбор нажатия на фронте молчит на
    нём сам (`dispatch.actionFor`: `bindings[a.id] ?? a.defaults`, а
    `[].includes(...)` — ложь). То есть выключение уже существовало в основании,
    не хватало способа туда попасть. Флаг `enabled` рядом дал бы два ответа на
    один вопрос — «сочетаний нет» и «выключено», — и они разъехались бы на первой
    же правке.

    ⚠ ЧТО ЛЕГКО СЛОМАТЬ ЗДЕСЬ. Отличие «выключено» от «действуют умолчания» —
    это отличие пустого списка от ОТСУТСТВИЯ ключа. Отфильтруй кто-нибудь пустые
    списки по дороге «за ненадобностью» — и сочетание тихо вернулось бы человеку
    при следующем входе, а он бы решил, что настройка не сохраняется.
    """
    resp = await _put(client, tokens["manager"], {"claim": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["hotkeys"] == {"claim": []}, "выключение не вернулось в ответе"

    заголовки = {"Authorization": f"Bearer {tokens['manager']}"}
    me = await client.get("/api/v1/auth/me", headers=заголовки)
    assert me.json()["hotkeys"] == {"claim": []}, (
        "выключение не пережило вход — человек решит, что настройка не сохраняется"
    )

    async with db_sessionmaker() as s:
        row = await s.get(User, users_by_role["manager"].id)
        assert row is not None and row.ui_settings == {"hotkeys": {"claim": []}}


async def test_выключить_можно_каждое_по_отдельности(client, tokens):
    """Выключение одного действия не трогает соседей.

    Иначе «выключить лишнее» означало бы «остаться без клавиш вовсе», и человек
    вернул бы всё по умолчанию, потеряв заодно свои переназначения.
    """
    resp = await _put(client, tokens["manager"], {"claim": [], "close": ["Mod+KeyY"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["hotkeys"] == {"claim": [], "close": ["Mod+KeyY"]}
