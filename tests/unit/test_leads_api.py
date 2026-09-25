"""Автозаявки: что отдаём расширению и чего не отдаём никогда.

Каждое свойство здесь ломается молча и стоит денег владельцу.

Отдали лид дважды — мастер поехал к человеку два раза. Отдали диалог без итога
«Выезд» — заявка на того, кто спросил цену и пропал. Придержали лид без телефона
и промолчали — потеряли две трети заявок так, что никто не узнает: расширение
исправно опрашивает, мы исправно отвечаем пустым списком.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AvitoAccount, Client, Conversation, Message
from app.models.lead import LeadHandout
from app.services import leads

TOKEN = "lead-token-for-tests-0123456789"


async def настроить(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        await leads.set_token(db, TOKEN)
        await db.commit()


def как_расширение() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def завести_выезд(
    session_factory: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
    *,
    phone: str | None = "+79161234567",
    src_key: str | None = "bt",
    city: str | None = "bryansk",
    title: str = "Канал",
    avito_user_id: int = 5550001,
    problem: str = "Не морозит холодильник, приедете?",
) -> uuid.UUID:
    """Диалог с итогом «Выезд» — то, что должно стать заявкой."""
    account = await make_avito_account(avito_user_id, title=title)
    async with session_factory() as db:
        acc = await db.get(AvitoAccount, account.id)
        assert acc is not None
        acc.lead_src_key = src_key
        client = Client(
            id=uuid.uuid4(),
            channel="avito",
            external_id=str(uuid.uuid4()),
            name="Сергей",
            phone=phone,
        )
        db.add(client)
        await db.flush()
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client.id,
            status="closed",
            outcome="visit",
            outcome_at=datetime.now(UTC),
            item_city_slug=city,
            item_title="Ремонт холодильников",
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body=problem,
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        return conv.id


# ------------------------------------------------------------------- доступ


async def test_without_token_the_door_is_shut(client: httpx.AsyncClient) -> None:
    """Не настроено — закрыто для всех, а не открыто для всех.

    Разница в одну строку кода и в доступ ко всем телефонам клиентов: ручка
    отвечает в интернет, и «пустой токен = пускаем» означало бы выгрузку базы
    номеров любому, кто угадал адрес.
    """
    assert (await client.get("/api/v1/leads")).status_code == 403


async def test_garbage_token_is_refused_not_a_crash(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """⚠ ЛЮБОЙ МУСОР В ЗАГОЛОВКЕ — ЧЕСТНЫЙ ОТКАЗ, А НЕ ПЯТИСОТКА.

    Поймано на стенде: `compare_digest` на строке с кириллицей БРОСАЕТ
    TypeError, и токен «подделка» давал 500 «Внутренняя ошибка». То есть чужой
    мусор выглядел как поломка сервера, и в мониторинге копились бы наши
    собственные пятисотки от чьего-то кривого запроса.
    """
    await настроить(db_sessionmaker)
    for плохой in ("wrong-token", "", "Bearer"):
        response = await client.get("/api/v1/leads", headers={"Authorization": f"Bearer {плохой}"})
        assert response.status_code == 401, f"{плохой!r} → {response.status_code}"

    # Кириллицу в заголовке проверяем НЕ по HTTP: `httpx` такой заголовок
    # отправить отказывается вовсе (заголовки — latin-1), а `curl` отправляет
    # сырыми байтами — и именно так дефект и всплыл на стенде. Поэтому зовём
    # проверку напрямую, тем же способом, каким её зовёт FastAPI.
    from app.api.routes.leads import require_lead_token
    from app.core.errors import ApiError

    async with db_sessionmaker() as db:
        with pytest.raises(ApiError) as поймано:
            await require_lead_token(db, authorization="Bearer подделка")
        assert поймано.value.status == 401


# --------------------------------------------------------------- что отдаём


async def test_visit_with_phone_becomes_a_lead(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Диалог с итогом «Выезд» и телефоном отдаётся целиком."""
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account)

    body = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert len(body["leads"]) == 1
    лид = body["leads"][0]

    assert лид["uid"] == str(conv_id)
    assert лид["srcKey"] == "bt"
    assert лид["phone"] == "+79161234567"
    # Город — РУССКИМ НАЗВАНИЕМ из слага Авито: лид-центр слаг не поймёт.
    assert лид["cityName"] == "Брянск"
    # Комментарий — словами клиента. Мастеру нужно, что сказал человек.
    assert "Не морозит холодильник" in лид["comments"]


@pytest.mark.parametrize("итог", ["declined", "not_our_profile", "spam", "no_reply", None])
async def test_other_outcomes_never_become_leads(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
    итог: str | None,
) -> None:
    """Заявка — только из «Выезда», который поставил человек.

    Остальные исходы заявкой не становятся по определению, а «не проставили»
    (NULL) — тем более: это диалог, о котором решение ещё не принято. Ошибись
    здесь — и в лид-центр поедет заявка на того, кто спросил цену и пропал,
    то есть возможный выезд мастера впустую.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account)
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        assert conv is not None
        conv.outcome = итог
        await db.commit()

    body = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert body["leads"] == []


@pytest.mark.parametrize(
    "чего_нет,поле",
    [("телефона", "phone"), ("лид-центра", "src_key"), ("города", "city")],
)
async def test_unfit_lead_is_held_back_not_lost(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
    чего_нет: str,
    поле: str,
) -> None:
    """Непригодный лид не отдаётся — и остаётся видимым, а не исчезает.

    На боевом из шести «Выездов» телефон есть у двух. Молчаливый пропуск
    остальных означал бы потерю двух третей заявок так, что никто никогда об
    этом не узнает.
    """
    await настроить(db_sessionmaker)
    аргументы: dict[str, str | None] = {}
    аргументы[поле] = None if поле != "city" else "такого-города-нет"
    if поле == "src_key":
        # ⚠ С 15.08 направление сперва читается из СЛОВ КЛИЕНТА, потом из
        # объявления (`lead_direction`). Чтобы случай проверял задуманное
        # («направления нет ниоткуда»), неопределимой должна быть и реплика.
        аргументы["problem"] = "Не включается, приедете?"
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account, **аргументы)  # type: ignore[arg-type]

    if поле == "src_key":
        # объявление тоже должно быть неопределимым — иначе оно даст направление
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            conv.item_title = "Услуга без узнаваемых слов"
            await db.commit()

    body = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert body["leads"] == [], f"лид без {чего_нет} не должен уходить в лид-центр"

    # И записи о выдаче не появилось: мы его не отдавали.
    async with db_sessionmaker() as db:
        assert (await db.execute(select(LeadHandout))).scalars().all() == []


async def test_held_back_leads_are_visible_on_the_screen(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Придержанное видно ТАМ, ГДЕ ЭКРАН ЭТО ОБЕЩАЕТ (28.08).

    Подпись раздела «Автозаявки» утверждает: «Если не сработали оба пути, заявка
    придерживается — и это видно в журнале ниже, а не пропадает молча».
    Обещание не выполнялось. `collect` складывает непригодные диалоги в
    `held_back` с причиной на каждый, но ручка выдачи писала их только в
    structlog агрегатом, а наружу отдавала одни лиды; строки `LeadHandout`
    придержанный не создаёт, поэтому и в журнале его быть не могло.

    По замеру в шапке `services/leads.py` из шести диалогов с итогом «Выезд»
    телефон есть у двух — две трети заявок висели невидимыми, и владелец,
    пришедший разбираться «почему в лид-центрах пусто», уходил ни с чем.

    ЧТО ЛОМАЛИ: убрали `held_back` из ответа ручки журнала — тест краснеет.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account, phone=None)

    ответ = await client.get(
        "/api/v1/settings/leads/handouts",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()

    придержаны = тело.get("held_back")
    assert придержаны, "экран обещает показать придержанные, а ручка их не отдаёт"
    строка = next(h for h in придержаны if h["conversation_id"] == str(conv_id))
    # Причина названа СВОИМИ словами: у «нет телефона» и «город не распознан»
    # разная починка, и общее «не получилось» их бы склеило.
    assert "телефон" in строка["reason"].lower()
    assert строка["account_title"]


# ------------------------------------------------------- однократность


async def test_the_same_lead_is_never_handed_out_twice(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """⚠ ДУБЛЬ ЗДЕСЬ — ЭТО ВТОРОЙ ВЫЕЗД МАСТЕРА К ТОМУ ЖЕ ЧЕЛОВЕКУ.

    Запись о выдаче делается сразу при отдаче, а не после подтверждения:
    иначе при любой заминке следующий опрос вернул бы тот же диалог.
    """
    await настроить(db_sessionmaker)
    await завести_выезд(db_sessionmaker, make_avito_account)

    первый = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    второй = (await client.get("/api/v1/leads", headers=как_расширение())).json()

    assert len(первый["leads"]) == 1
    assert второй["leads"] == []


async def test_unacked_lead_returns_after_an_hour(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Неподтверждённый лид не теряется навсегда.

    Подтверждение расширение шлёт «глотая ошибку» — чтобы недоступность
    LeadChat не мешала заводить заявки. Значит оно может не дойти, и без
    повторной выдачи лид завис бы навсегда, а заявка не создалась бы вовсе.
    """
    await настроить(db_sessionmaker)
    await завести_выезд(db_sessionmaker, make_avito_account)
    assert len((await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"]) == 1

    async with db_sessionmaker() as db:
        row = (await db.execute(select(LeadHandout))).scalar_one()
        row.handed_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()

    снова = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert len(снова["leads"]) == 1


async def test_acked_lead_never_returns(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Подтверждённый лид не выдаётся снова — даже если лид-центр отклонил.

    Причина отказа сама не рассосётся, а повтор каждые пять минут превратится
    в поток одинаковых ошибок, за которым перестанут следить.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account)
    await client.get("/api/v1/leads", headers=как_расширение())

    ответ = await client.post(
        f"/api/v1/leads/{conv_id}/ack",
        headers=как_расширение(),
        json={"uid": str(conv_id), "decision": "error", "message": "город не найден"},
    )
    assert ответ.status_code == 200

    async with db_sessionmaker() as db:
        row = (await db.execute(select(LeadHandout))).scalar_one()
        row.handed_at = datetime.now(UTC) - timedelta(days=3)
        await db.commit()

    assert (await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"] == []


async def test_ack_of_unknown_lead_answers_ok(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """⚠ НЕИЗВЕСТНЫЙ ЛИД ПОДТВЕРЖДАЕТСЯ 200, А НЕ 404.

    Расширение шлёт подтверждение ПОСЛЕ создания заявки и ошибку глотает.
    Ответь мы 404 — оно бы промолчало, лид остался бы неподтверждённым и ушёл
    в повторную выдачу. То есть строгость здесь стоила бы второй заявки и
    второго выезда.
    """
    await настроить(db_sessionmaker)
    ответ = await client.post(
        f"/api/v1/leads/{uuid.uuid4()}/ack",
        headers=как_расширение(),
        json={"decision": "new", "requestId": 1},
    )
    assert ответ.status_code == 200
    assert ответ.json()["known"] is False


# ------------------------------------------------ телефон заявки по диалогу


class TestТелефонБерётсяПоДиалогу:
    """⚠ ОБЪЕДИНЁННАЯ КАРТОЧКА НЕ ИМЕЕТ ПРАВА УВЕЗТИ МАСТЕРА К ПОСТОРОННЕМУ.

    Заявка собиралась из карточки — `client.phone`. Пока карточка описывает одного
    человека, это верно. Но карточки объединяются (руками, а с правкой 14.08 и
    автоматически), и после объединения в `phone` может лежать номер, названный в
    СОСЕДНЕМ диалоге. Заявка уходит в лид-центр, мастер едет к другому человеку.

    Это худшее последствие ошибочной склейки: показ лишней истории на экране
    неприятен и обратим, выезд — нет. Граница закрывается здесь, отдельно от самой
    связки и раньше неё: даже если объединение ошиблось, наружу уйдёт либо верный
    номер, либо ничего.

    ⚠ ПОЧЕМУ ЭТИ ТЕСТЫ ОБЯЗАТЕЛЬНЫ. Правку можно снять одной строкой — вернуть
    `client.phone` в `_lead_payload`, — и все 150 прежних тестов заявок останутся
    зелёными: они собирают карточку с одним диалогом, где оба пути дают один и тот же
    номер. Разница видна ТОЛЬКО на объединённой карточке, а такой в них нет ни одной.
    """

    async def test_необъединённая_карточка_отдаёт_свой_телефон(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Совместимость: у всех строк до миграции 0044 происхождение неизвестно.

        Без этого пути каждая сегодняшняя заявка встала бы в тот же миг, когда
        правка выкатится, — а `phone_conversation_id` заполняется только вперёд.
        """
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(db_sessionmaker, make_avito_account)
        r = await client.get("/api/v1/leads", headers=как_расширение())
        assert r.status_code == 200, r.text
        заявки = {л["uid"]: л for л in r.json()["leads"]}
        assert заявки[str(conv_id)]["phone"] == "+79161234567"

    async def test_объединённая_карточка_с_чужим_номером_не_отдаёт_ничего(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Номер есть, но доказан не этим диалогом — заявка придерживается.

        Это и есть сценарий «звоните жене»: карточка мужа влилась в карточку жены,
        в `phone` лежит номер, введённый в её диалоге, а выезд — по его.
        """
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(db_sessionmaker, make_avito_account)
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            жертва = await db.get(Client, conv.client_id)
            assert жертва is not None
            # Кто-то влился в эту карточку — значит она объединённая.
            проигравшая = Client(
                id=uuid.uuid4(),
                channel="avito",
                external_id=str(uuid.uuid4()),
                name="Ольга",
                merged_into_id=жертва.id,
            )
            db.add(проигравшая)
            # Номер доказан ЧУЖИМ диалогом.
            жертва.phone_conversation_id = uuid.uuid4()
            await db.commit()

        r = await client.get("/api/v1/leads", headers=как_расширение())
        assert r.status_code == 200, r.text
        uids = {л["uid"] for л in r.json()["leads"]}
        assert str(conv_id) not in uids, (
            "заявка ушла с номером, доказанным другим диалогом — мастер поедет к постороннему"
        )

    async def test_объединённая_карточка_с_номером_этого_диалога_отдаёт_его(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Объединение само по себе заявку не останавливает — останавливает НЕЯСНОСТЬ.

        Если номер доказан именно этим диалогом, объединение ничему не мешает: мы
        знаем, чей это телефон.
        """
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(db_sessionmaker, make_avito_account)
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            карточка = await db.get(Client, conv.client_id)
            assert карточка is not None
            db.add(
                Client(
                    id=uuid.uuid4(),
                    channel="avito",
                    external_id=str(uuid.uuid4()),
                    name="Ольга",
                    merged_into_id=карточка.id,
                )
            )
            карточка.phone_conversation_id = conv_id
            await db.commit()

        r = await client.get("/api/v1/leads", headers=как_расширение())
        заявки = {л["uid"]: л for л in r.json()["leads"]}
        assert str(conv_id) in заявки
        assert заявки[str(conv_id)]["phone"] == "+79161234567"


class TestНаправлениеРешаетПроблема:
    """⚠ Направление: ПРОБЛЕМА клиента → объявление → настройка канала.

    Владелец 15 августа: «бот должен определять направление исходя из проблемы
    клиента» (перевернуло порядок 14.08 «читаем объявление»): в живых диалогах
    пишут про стиральную машину в чат про компьютеры — в ближайший открытый канал.

    ⚠ ПОЧЕМУ ОСТАЛЬНЫЕ ТЕСТЫ ЭТОГО НЕ ЛОВЯТ. У них в заготовке реплика, объявление и
    настройка канала указывают на ОДНО направление, и все пути дают одинаковый ответ.
    Различить можно только там, где они РАСХОДЯТСЯ, — здесь.
    """

    async def test_проблема_перебивает_объявление(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Пишут про стиралку (БТ) в объявление про принтеры (КП) — заявка про стиралку."""
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(
            db_sessionmaker,
            make_avito_account,
            src_key="kp",
            problem="Стиральная машина не сливает воду, приедете?",
        )
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            conv.item_title = "Ремонт принтеров и мфу / Не печатает, зажёвывает бумагу"
            await db.commit()

        заявки = (await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"]
        лид = next(л for л in заявки if л["uid"] == str(conv_id))
        assert лид["srcKey"] == "bt", (
            "заявка про стиралку ушла по объявлению в компьютерный лид-центр — "
            "мастер по компьютерам к стиралке не поедет"
        )

    async def test_объявление_перебивает_настройку_канала(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Реплика без слов техники, канал — бытовая техника, объявление — принтеры."""
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(
            db_sessionmaker,
            make_avito_account,
            src_key="bt",
            problem="Не включается, приедете?",
        )
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            conv.item_title = "Ремонт принтеров и мфу / Не печатает, зажёвывает бумагу"
            await db.commit()

        заявки = (await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"]
        лид = next(л for л in заявки if л["uid"] == str(conv_id))
        assert лид["srcKey"] == "kp", (
            "заявка про принтер ушла в лид-центр канала (бытовая техника) — "
            "мастер по технике к компьютерной задаче не поедет"
        )

    async def test_неизвестное_объявление_берёт_настройку_канала(
        self, client, db_sessionmaker, make_avito_account
    ):
        """Запасной путь обязан работать: иначе заявки встанут на первом же новом виде услуг."""
        await настроить(db_sessionmaker)
        conv_id = await завести_выезд(
            db_sessionmaker,
            make_avito_account,
            src_key="mnc",
            problem="Не включается, приедете?",
        )
        async with db_sessionmaker() as db:
            conv = await db.get(Conversation, conv_id)
            assert conv is not None
            conv.item_title = "Услуга без узнаваемых слов"
            await db.commit()

        заявки = (await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"]
        лид = next(л for л in заявки if л["uid"] == str(conv_id))
        assert лид["srcKey"] == "mnc"


async def test_the_hold_reason_names_both_failed_paths(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Причина придержки называет ОБА несработавших пути, а не один.

    ⚠ Владелец дважды (14 и 15 августа) прочитал старую формулировку «у канала
    не выбран лид-центр» как «у аккаунта нет направления» — и был прав: текст
    звал чинить настройку там, где направление давно решает объявление. Причина
    обязана говорить, что не сработали оба пути: объявление не распозналось И
    запасного выбора нет. Проверяется сама строка: она уезжает в журнал экрана
    «Заявки», это текст для человека.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account, src_key=None)
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        assert conv is not None
        conv.item_title = "Услуга без узнаваемых слов"
        await db.commit()

    (await client.get("/api/v1/leads", headers=как_расширение())).json()

    from app.services.leads import HeldBack

    assert "по объявлению" in HeldBack.NO_SRC
    assert "запасной" in HeldBack.NO_SRC


async def test_белая_заявка_723_на_бт_придерживается(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Регламент 15.08, п. 3: белые заявки (партнёр 723) на БТ не создаются.

    Клиент пишет про стиральную машину в белый канал — заявка не уезжает в
    лид-центр БТ, а висит в придержанных с причиной: человек решит, куда её.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(
        db_sessionmaker,
        make_avito_account,
        src_key="kp",
        problem="Стиральная машина не сливает воду, приедете?",
    )
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        assert conv is not None
        acc = await db.get(AvitoAccount, conv.account_id)
        assert acc is not None
        acc.lead_partner_number = "723"
        await db.commit()

    body = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert body["leads"] == [], "белая заявка про стиралку уехала в лид-центр БТ"

    # и записи о выдаче нет: заявка придержана, а не потеряна
    async with db_sessionmaker() as db:
        assert (await db.execute(select(LeadHandout))).scalars().all() == []

    from app.services.leads import HeldBack

    assert "723" in HeldBack.WHITE_BT and "БТ" in HeldBack.WHITE_BT


async def test_retries_stop_after_a_day_of_silence(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Повторы не вечны: сутки без подтверждения — и лид перестаёт уезжать.

    БЫЛО (аудит 19.08, находка L-005): счётчика попыток не было вовсе, и
    неподтверждённый лид отдавался заново КАЖДЫЙ ЧАС бесконечно, а `handed_at`
    перезаписывался — история стиралась, и в журнале выдач это выглядело одной
    строкой. Если расширение сломано или ключ не тот, поток одинаковых повторов
    мешал бы увидеть настоящие лиды.
    """
    await настроить(db_sessionmaker)
    await завести_выезд(db_sessionmaker, make_avito_account)
    assert len((await client.get("/api/v1/leads", headers=как_расширение())).json()["leads"]) == 1

    async with db_sessionmaker() as db:
        row = (await db.execute(select(LeadHandout))).scalar_one()
        assert row.attempts == 1, "первая выдача обязана считаться попыткой"
        row.handed_at = datetime.now(UTC) - timedelta(hours=2)
        row.attempts = 24  # сутки почасовых повторов позади
        await db.commit()

    снова = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert снова["leads"] == [], "после суток молчания лид повторять нечем"


async def test_attempts_are_counted_not_overwritten(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Каждая повторная выдача видна числом, а не догадкой по журналу."""
    await настроить(db_sessionmaker)
    await завести_выезд(db_sessionmaker, make_avito_account)
    await client.get("/api/v1/leads", headers=как_расширение())

    async with db_sessionmaker() as db:
        row = (await db.execute(select(LeadHandout))).scalar_one()
        row.handed_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()

    await client.get("/api/v1/leads", headers=как_расширение())

    async with db_sessionmaker() as db:
        row = (await db.execute(select(LeadHandout))).scalar_one()
        assert row.attempts == 2, "вторая выдача обязана увеличить счётчик"


async def test_old_visits_are_not_dragged_out_of_history(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> None:
    """Выборка лидов не тянет весь исторический хвост.

    БЫЛО: ни границы по времени, ни лимита. Первый же опрос после выпуска
    токена отдал бы КАЖДУЮ старую заявку заново — то есть мастера, выехавшего
    дважды. При 86 диалогах незаметно, при 3000 в сутки — тысячи строк разом.
    """
    await настроить(db_sessionmaker)
    conv_id = await завести_выезд(db_sessionmaker, make_avito_account)

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        conv.outcome_at = datetime.now(UTC) - timedelta(days=30)
        await db.commit()

    ответ = (await client.get("/api/v1/leads", headers=как_расширение())).json()
    assert ответ["leads"] == [], "заявка месячной давности не должна уезжать заново"


async def test_repeat_handout_reads_rows_under_lock() -> None:
    """Строки выданных заявок читаются под замком (аудит 19.08, находка L-006).

    ПОЧЕМУ ЭТО ВАЖНО ИМЕННО НА ВЕТКЕ ПОВТОРА. Первичная выдача прикрыта
    уникальным ограничением по диалогу: два одновременных опроса не заведут две
    строки. А ветка ПОВТОРНОЙ выдачи (строка есть, подтверждения нет, час
    прошёл) — это SELECT, потом UPDATE, и между ними помещается второй опрос:
    оба читают одну и ту же неподтверждённую строку, оба решают «пора отдать
    заново», и один лид уезжает в CRM дважды. Двум вкладкам расширения или
    перезапуску по таймеру для этого ничего особенного делать не надо.

    Проверяем скомпилированный SQL на диалекте прода: `with_for_update()` на
    SQLite молча пропадает, поэтому обычный тест на данных этого не поймал бы
    НИКОГДА — он зелёный и с замком, и без него.

    Проверка ломанием: уберите `.with_for_update()` в `_already_handed` —
    тест краснеет.
    """
    from sqlalchemy.dialects import postgresql

    запросы: list[object] = []

    class _Пустой:
        def all(self) -> list[object]:
            return []

    class _Ответ:
        def scalars(self) -> _Пустой:
            return _Пустой()

    class _Сессия:
        async def execute(self, stmt: object) -> _Ответ:
            запросы.append(stmt)
            return _Ответ()

    # ⚠ СПИСОК ДИАЛОГОВ ОБЯЗАТЕЛЕН С 27.08. Раньше замок брался на ВСЮ таблицу
    # выдач — `select(LeadHandout).with_for_update()` без единого условия, — и
    # держался весь сбор порции: до двухсот диалогов с запросами по каждому.
    # Подтверждения заявок всё это время ждали на том же замке. Теперь замок
    # сужен до строк текущей порции, и пустой список означает «блокировать
    # нечего».
    await leads._already_handed(_Сессия(), datetime.now(UTC), [uuid.uuid4()])  # type: ignore[arg-type]

    assert len(запросы) == 1
    sql = str(запросы[0].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert "FOR UPDATE" in sql, (
        "выданные заявки читаются без замка: два одновременных опроса отдадут "
        f"один лид дважды. SQL: {sql}"
    )
    assert "IN (" in sql or "= ANY" in sql, (
        "замок снова на всей таблице выдач: подтверждения заявок будут ждать "
        f"весь сбор порции. SQL: {sql}"
    )


async def test_empty_batch_locks_nothing() -> None:
    """Диалогов в порции нет — блокировать нечего, и запроса быть не должно."""
    запросы: list[object] = []

    class _Сессия:
        async def execute(self, stmt: object) -> object:  # pragma: no cover - не зовётся
            запросы.append(stmt)
            raise AssertionError("замок взят на пустой порции")

    итог = await leads._already_handed(_Сессия(), datetime.now(UTC), [])  # type: ignore[arg-type]
    assert итог == {}
    assert запросы == []


# --------------------------------------- предохранитель второго пути (L-005)


class TestПредохранительВторогоПути:
    """Дверей к выдаче токена две, и закрыты они обязаны быть обе.

    Заявки в CRM создаёт очередь ЛИД-БОТА через расширение «Автозаявки».
    Очередь LeadChat (`GET /leads`) ведёт в то же расширение и те же
    бот-диалоги: включи её кто-нибудь — каждая заявка уехала бы в лид-центр
    дважды, то есть мастер выехал бы дважды по одному адресу.

    16 августа владелец закрыл этот путь. Закрыли, однако, только ручку —
    команда `python -m app.cli leads-token` осталась открытой, а живёт она на
    сервере, то есть ровно там, куда пойдёт человек, которому «надо просто
    подключить расширение».
    """

    async def test_ручка_отказывает_и_объясняет_почему(
        self, client: httpx.AsyncClient, tokens: dict[str, str]
    ) -> None:
        ответ = await client.post(
            "/api/v1/settings/leads/token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert ответ.status_code == 409
        тело = ответ.json()["error"]
        assert тело["code"] == "leads_disabled"
        assert тело["message"] == leads.SECOND_PATH_FUSE, (
            "объяснение разъехалось с предохранителем — человек прочтёт одно, а сработает другое"
        )

    def test_команда_на_сервере_закрыта_тем_же_предохранителем(self, capsys) -> None:
        from app import cli

        with pytest.raises(typer.Exit) as отказ:
            cli.leads_token(revoke=False)
        assert отказ.value.exit_code == 2
        напечатано = capsys.readouterr().out
        assert leads.SECOND_PATH_FUSE in напечатано, (
            "команда закрылась молча: человек увидит непонятный код возврата и "
            "пойдёт искать обход вместо того, чтобы прочитать причину"
        )

    async def test_снятый_предохранитель_действительно_выпускает_токен(
        self,
        client: httpx.AsyncClient,
        tokens: dict[str, str],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """День переключения обязан работать СЕГОДНЯ, а не «когда снимем».

        Выдачу токена однажды уже выбросили — заменили телом `raise`. Тогда
        снятие предохранителя дало бы не работающий путь, а 500 в ответ на
        кнопку, и разбираться в этом пришлось бы в самый неподходящий день.
        Здесь код выдачи проверяется при снятом предохранителе — то есть
        ровно тот путь, ради которого его и держат живым.
        """
        monkeypatch.setattr(leads, "SECOND_PATH_FUSE", None)
        ответ = await client.post(
            "/api/v1/settings/leads/token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert ответ.status_code == 200, ответ.text
        выданный = ответ.json()["token"]
        assert выданный, "токен пустой — расширению нечего вписывать"

        async with db_sessionmaker() as db:
            assert await leads.get_token(db) == выданный, (
                "показали одно, а сохранили другое — расширение получит 401"
            )

    def test_отзыв_предохранителем_не_закрыт(self) -> None:
        """Закрыть путь можно всегда — иначе предохранитель стал бы ловушкой."""
        from app import cli

        источник = inspect.getsource(cli.leads_token)
        assert "if not revoke and" in источник, (
            "предохранитель начал мешать ОТЗЫВУ токена: выключить автозаявки "
            "стало нельзя, а это единственная кнопка «стоп»"
        )
