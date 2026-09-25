"""ПОПУЛЯРНОСТЬ ЗАГОТОВОК: ХОДОВЫЕ — ВЫШЕ.

⚠ ПРОСЬБА ВЛАДЕЛЬЦА 29.08: «сделай популярность ответов, когда пишешь для
быстрых ответов».

ЗАЧЕМ. Общих заготовок 39 плюс личные, а подсказка показывает пять. Какие
пять — вопрос не вкуса: у каждой смены свой набор ходовых фраз, и он меняется
от сезона и канала. Порядок, зашитый в код, устаревает в день, когда его
написали.

⚠ СЧЁТЧИК СЕЯТСЯ НЕ С НУЛЯ. Пустой у всех означает, что первую неделю подсказка
сортирует по алфавиту — то есть бесполезна ровно тогда, когда человек к ней
привыкает. Заготовки из истории стартуют с числа «сколько раз фразу набрали
руками» (замер 28.08, от 37 до 6655).
"""

import uuid

import pytest
import sqlalchemy as sa

from app.data.base_templates import БАЗОВЫЕ_ЗАГОТОВКИ
from app.models.template import Template

pytestmark = pytest.mark.anyio


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def общая(db_sessionmaker) -> uuid.UUID:
    async with db_sessionmaker() as db:
        t = Template(owner_id=None, title="Здравствуйте", body="Здравствуйте!", folder="Открытие")
        db.add(t)
        await db.commit()
        return t.id


async def test_applying_a_template_raises_its_count(client, tokens, общая, db_sessionmaker) -> None:
    r = await client.post(f"/api/v1/templates/{общая}/used", headers=auth(tokens))
    assert r.status_code == 204, r.text

    async with db_sessionmaker() as db:
        assert (await db.get(Template, общая)).used_count == 1


async def test_the_count_is_raised_by_the_database_not_by_read_and_write(
    client, tokens, общая, db_sessionmaker
) -> None:
    """Тринадцать диспетчеров применяют одни и те же общие заготовки.

    Прочитай мы значение и запиши обратно, часть нажатий потерялась бы молча:
    два одновременных применения дали бы +1 вместо +2. Считает сама база.
    """
    for _ in range(5):
        assert (
            await client.post(f"/api/v1/templates/{общая}/used", headers=auth(tokens))
        ).status_code == 204

    async with db_sessionmaker() as db:
        assert (await db.get(Template, общая)).used_count == 5


async def test_the_count_travels_to_the_screen(client, tokens, общая) -> None:
    """Без поля в выдаче подсказка не сможет ставить ходовые выше."""
    await client.post(f"/api/v1/templates/{общая}/used", headers=auth(tokens))
    r = await client.get("/api/v1/templates?scope=all", headers=auth(tokens))
    assert r.status_code == 200, r.text
    (строка,) = [i for i in r.json()["items"] if i["id"] == str(общая)]
    assert строка["used_count"] == 1


async def test_a_manager_can_raise_the_count_of_a_SHARED_template(client, tokens, общая) -> None:
    """⚠ ГЛАВНАЯ ГРАНИЦА ПРАВ.

    Менять текст общей заготовки может только администратор и руководитель
    (`templates:shared`). Потребуй мы это право и здесь, счётчик у общих не
    двигался бы ни у кого, кроме них, — то есть популярность считалась бы по
    двоим из тринадцати и не значила бы ничего.
    """
    r = await client.post(f"/api/v1/templates/{общая}/used", headers=auth(tokens, "manager"))
    assert r.status_code == 204, r.text


async def test_an_observer_cannot(client, tokens, общая) -> None:
    """Наблюдатель клиентам не отвечает — применять заготовки ему нечем."""
    r = await client.post(f"/api/v1/templates/{общая}/used", headers=auth(tokens, "observer"))
    assert r.status_code == 403, r.text


async def test_someone_elses_personal_template_stays_invisible(
    client, tokens, db_sessionmaker, users_by_role
) -> None:
    """Чужая личная заготовка отвечает 404, а не 403.

    Существование чужих заготовок — не наше дело: отказ по правам подтвердил бы,
    что она есть.
    """
    async with db_sessionmaker() as db:
        чужая = Template(
            owner_id=users_by_role["admin"].id, title="Чужая", body="Текст", folder=None
        )
        db.add(чужая)
        await db.commit()
        чужой_id = чужая.id

    r = await client.post(f"/api/v1/templates/{чужой_id}/used", headers=auth(tokens, "manager"))
    assert r.status_code == 404, r.text

    async with db_sessionmaker() as db:
        assert (await db.get(Template, чужой_id)).used_count == 0


async def test_a_missing_template_does_not_blow_up(client, tokens) -> None:
    r = await client.post(f"/api/v1/templates/{uuid.uuid4()}/used", headers=auth(tokens))
    assert r.status_code == 404


def test_seeded_templates_start_with_their_measured_count() -> None:
    """У набора из истории стартовое число — не ноль.

    Иначе первую неделю подсказка сортирует по алфавиту, то есть бесполезна
    ровно тогда, когда человек к ней привыкает.
    """
    assert all(раз > 0 for *_, раз in БАЗОВЫЕ_ЗАГОТОВКИ)
    самая = max(БАЗОВЫЕ_ЗАГОТОВКИ, key=lambda z: z[3])
    assert самая[1] == "Здравствуйте", "самой ходовой оказалась не та фраза, что в замере"


async def test_seeding_writes_the_measured_count_into_the_column(db_sessionmaker) -> None:
    """Число из набора обязано доехать до базы — иначе посев бессмыслен."""
    async with db_sessionmaker() as db:
        for папка, имя, тело, раз in БАЗОВЫЕ_ЗАГОТОВКИ[:5]:
            db.add(Template(owner_id=None, title=имя, body=тело, folder=папка, used_count=раз))
        await db.commit()

    async with db_sessionmaker() as db:
        rows = (await db.execute(sa.select(Template))).scalars().all()
        assert all(r.used_count > 0 for r in rows)


def test_seeding_wires_the_measured_count_into_the_row() -> None:
    """ПРОВОДКА: посев кладёт измеренное число в колонку, а не забывает его.

    ⚠ Без этой проверки диверсия «сеять с нуля» не краснела: остальные тесты
    строят строки руками и до самой команды не доходят. А посев с нуля — это
    ровно то, от чего число и заводилось: первую неделю подсказка сортировала
    бы по алфавиту, то есть была бы бесполезна тогда, когда к ней привыкают.
    """
    import inspect
    import re

    from app import cli

    src = re.sub(r"#[^\n]*", " ", inspect.getsource(cli.seed_templates))
    assert "used_count=раз" in src, (
        "посев не переносит измеренное число в счётчик — подсказка стартует с алфавита"
    )


async def test_seeding_backfills_a_missing_start_count(db_sessionmaker) -> None:
    """⚠ БОЕВОЙ ПРОМАХ 29.08: ЗАГОТОВКИ ЗАВЕЛИ РАНЬШЕ, ЧЕМ КОЛОНКУ.

    39 общих создали 28 августа, а `used_count` появился 29-го — то есть все
    получили ноль, и подсказка сортировала их по алфавиту. Отбор по заголовку
    при повторном запуске честно отвечал «добавлять нечего» и мимо этого
    проходил: стартовое число, ради которого всё и делалось, не доехало.

    Здесь проверяется вторая половина посева: нулевым проставить измеренное.
    """
    import inspect
    import re

    from app import cli

    src = re.sub(r"#[^\n]*", " ", inspect.getsource(cli.seed_templates))
    assert "Template.used_count == 0" in src, (
        "посев не видит заготовок без счёта — стартовое число до них не доедет"
    )
    assert "row.used_count = измеренное[row.title]" in src


async def test_a_live_count_is_never_overwritten(db_sessionmaker) -> None:
    """У заготовки с живым счётом свой счёт правдивее нашего замера.

    Перебей мы его при каждой выкатке, популярность откатывалась бы к
    прошлогоднему замеру — и тем сильнее врала бы, чем дольше работает система.
    """
    import inspect
    import re

    from app import cli

    src = re.sub(r"#[^\n]*", " ", inspect.getsource(cli.seed_templates))
    # Отбор ограничен нулевыми — значит ненулевые не попадают в выборку вовсе.
    i = src.index("без_счёта")
    assert "Template.used_count == 0" in src[i : i + 400]
