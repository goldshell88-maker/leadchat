"""Модель как второй читатель адреса (владелец 13.09: «пусть бесплатные модели
помогают»).

⚠ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТ ФАЙЛ: ответ модели — предложение, а не запись. Слово,
которого нет в репликах клиента, в карточку не попадает (сторож цитаты);
дом, склеенный с квартирой («17/3кв 4»), не берётся; телефон клиента наружу
не уходит; без ключа и при выключателе модель не зовётся; потолок в сутки
считается по запросам, включая запросы к следующей модели.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
import structlog

from app.core.config import settings
from app.integrations import gateway, openrouter
from app.models import ClientAddressCandidate, Conversation, Message
from app.services import address_llm, address_parse, app_settings
from app.workers import address_llm as worker

pytestmark = pytest.mark.anyio

ЗЕЛЕНОГОРСКАЯ = "Можно будет часов 11 улица Зеленогорская 17/3кв 4"


def ответ(**поля: str) -> dict[str, Any]:
    базовый = {
        "street": "",
        "house": "",
        "settlement": "",
        "settlement_type": "",
        "city": "",
        "apartment": "",
        "entrance": "",
        "floor": "",
        "intercom": "",
        "confidence": "high",
        "note": "",
    }
    базовый.update(поля)
    return базовый


def test_чтение_модели_проходит_сторож_цитаты() -> None:
    чтение = address_llm.parse_reading(
        ответ(street="улица Зеленогорская", house="17/3", apartment="4")
    )
    assert чтение is not None
    found = address_llm.to_found(чтение, [ЗЕЛЕНОГОРСКАЯ])
    assert found is not None
    assert (found.street, found.house, found.parts) == (
        "улица Зеленогорская",
        "17/3",
        {"office": "4"},
    )
    # Цитата — окно от улицы до дома, а не вся реплика (в ней бывает лишнее).
    assert found.level == address_parse.LEVEL_A and found.raw == "улица Зеленогорская 17/3"
    assert address_parse.quote_holds(found, ЗЕЛЕНОГОРСКАЯ)


def test_выдуманная_буква_не_проходит() -> None:
    """«Зеленоградская» вместо «Зеленогорская» — модель подставила букву: отказ."""
    чтение = address_llm.parse_reading(ответ(street="улица Зеленоградская", house="17/3"))
    assert чтение is not None
    assert address_llm.to_found(чтение, [ЗЕЛЕНОГОРСКАЯ]) is None


def test_дом_склеенный_с_квартирой_не_берётся() -> None:
    assert address_llm.parse_reading(ответ(street="Ленина", house="17/3кв 4")) is None
    assert address_llm.parse_reading(ответ(street="Ленина", house="18 этажей")) is None
    assert address_llm.parse_reading(ответ(confidence="none")) is None
    assert address_llm.parse_reading(ответ(street="Ленина", house="5")) is not None


def test_город_и_части_из_чтения() -> None:
    реплика = "Бердск микрорайон 23 кв6 2 подъезд этаж кв домофон работает 8900XXXXXXX"
    чтение = address_llm.parse_reading(
        ответ(street="микрорайон", house="23", city="Бердск", apartment="6", entrance="2")
    )
    assert чтение is not None
    found = address_llm.to_found(чтение, [реплика])
    assert found is not None
    assert (found.street, found.house, found.locality) == ("микрорайон", "23", "Бердск")
    assert found.parts == {"office": "6", "entrance": "2"}


def test_улица_и_дом_в_разных_репликах_склеиваются() -> None:
    чтение = address_llm.parse_reading(ответ(street="ул Горького", house="5"))
    assert чтение is not None
    found = address_llm.to_found(чтение, ["в Королёве ул. Горького", "дом 5"])
    assert found is not None and found.raw == "ул. Горького; дом 5"
    # Улица без дома — место.
    без_дома = address_llm.parse_reading(ответ(street="ул Горького", house=""))
    assert без_дома is not None
    место = address_llm.to_found(без_дома, ["в Королёве ул. Горького"])
    assert место is not None and место.kind == address_parse.KIND_PLACE


def test_телефон_наружу_не_уходит() -> None:
    текст = address_llm.build_user_message(["Ленина 5, звоните 8 900 123 45 67", "кв 3"], "Бердск")
    assert "123" not in текст and "45 67" not in текст
    assert "Город объявления: Бердск" in текст and "— кв 3" in текст


def test_похоже_на_адрес() -> None:
    assert address_llm.looks_like_address("Можно будет часов 11 Зеленогорская 17/3")
    assert address_llm.looks_like_address("мой адрес такой")
    assert address_llm.looks_like_address("ул Ленина")
    assert not address_llm.looks_like_address("Привет, сколько стоит?")
    assert not address_llm.looks_like_address("ок")


def _читатели(monkeypatch: pytest.MonkeyPatch, есть: bool) -> None:
    """Ключи читателей живут на шлюзе (docs/46): тест задаёт снимок `/status`."""
    assert gateway.known_keys is not None
    for имя in openrouter.READERS:
        monkeypatch.setitem(gateway.known_keys, имя, есть)


GW_CHAT = "http://gw.test/llm/chat"


@respx.mock
async def test_обёртка_ходит_в_шлюз_и_считает_попытки(monkeypatch) -> None:
    """Сам перебор моделей проверяет gateway/tests/test_gw_llm.py; здесь — что
    обёртка отдаёт шлюзу и как считает запросы для суточного потолка."""
    _читатели(monkeypatch, True)
    счёт = 0
    увидел_до_похода: list[int] = []

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    def обработчик(request: httpx.Request) -> httpx.Response:
        увидел_до_похода.append(счёт)
        тело = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert тело == {"system": "s", "user": "u", "deadline_sec": openrouter.DEADLINE_SEC}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "content": ответ(street="ул Ленина", house="5"),
                "model": "b/two:free",
                "attempts": 3,
            },
        )

    маршрут = respx.post(GW_CHAT).mock(side_effect=обработчик)
    данные, модель = await openrouter.chat_json("s", "u", on_request=посчитать)
    assert данные["street"] == "ул Ленина" and модель == "b/two:free"
    # Один раз до похода (потолок считает запросы, а не ответы), ещё два —
    # по числу запросов, которые шлюз сделал к читателям.
    assert увидел_до_похода == [1] and счёт == 3
    assert маршрут.call_count == 1

    # Без счётчика — просто ответ; клиент из теста доходит до шлюза.
    async with httpx.AsyncClient() as client:
        данные, _ = await openrouter.chat_json("s", "u", client=client)
    assert данные["house"] == "5"


@respx.mock
async def test_отказы_шлюза_и_читателей_становятся_OpenRouterError(monkeypatch) -> None:
    _читатели(monkeypatch, True)
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    # Все модели отказали: kind и статус шлюза как есть; число попыток при
    # отказе едет в `GatewayError.extra` — потолок считает и их (4 запроса).
    respx.post(GW_CHAT).mock(
        return_value=httpx.Response(
            200, json={"ok": False, "kind": "exhausted", "status": 429, "attempts": 4}
        )
    )
    with pytest.raises(openrouter.OpenRouterError) as exc:
        await openrouter.chat_json("s", "u", on_request=посчитать)
    assert (exc.value.kind, exc.value.status) == ("exhausted", 429) and счёт == 4

    # Ответ не той формы — bad_response.
    respx.post(GW_CHAT).mock(return_value=httpx.Response(200, json={"ok": True, "model": "m"}))
    with pytest.raises(openrouter.OpenRouterError) as exc:
        await openrouter.chat_json("s", "u")
    assert exc.value.kind == "bad_response"

    # Шлюз недоступен или не принял токен — network: те же кулдауны, что при
    # «карта не отвечает»; переписка в лог не попадает.
    respx.post(GW_CHAT).mock(side_effect=httpx.ConnectTimeout("boom"))
    with structlog.testing.capture_logs() as логи:
        with pytest.raises(openrouter.OpenRouterError) as exc:
            await openrouter.chat_json("s", "Ленина 5, кв 3")
    assert exc.value.kind == "network"
    assert "Ленина" not in json.dumps(логи, ensure_ascii=False)
    respx.post(GW_CHAT).mock(return_value=httpx.Response(401, json={"detail": "x"}))
    with pytest.raises(openrouter.OpenRouterError) as exc:
        await openrouter.chat_json("s", "u")
    assert exc.value.kind == "network"

    # На шлюзе нет ни одного ключа читателей: blocked, и `enabled()` гаснет
    # для всех троих до следующего снимка — без нового похода.
    маршрут = respx.post(GW_CHAT).mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "attempts": 0})
    )
    assert openrouter.enabled()
    with pytest.raises(openrouter.OpenRouterError) as exc:
        await openrouter.chat_json("s", "u")
    assert exc.value.kind == "blocked"
    assert not openrouter.enabled()
    assert gateway.known_keys == {"openrouter": False, "groq": False, "mistral": False}
    походов = маршрут.call_count
    with pytest.raises(openrouter.OpenRouterError) as exc:
        await openrouter.chat_json("s", "u")
    assert exc.value.kind == "blocked" and маршрут.call_count == походов

    # Ключ появился у одного из запасных — узнаём из нового снимка `/status`
    # (память «ключа нет» живёт до него), и читатели снова включены.
    respx.get("http://gw.test/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "providers": {
                    "openrouter": {"key_present": False, "base_url": "u"},
                    "groq": {"key_present": False, "base_url": "u"},
                    "mistral": {"key_present": True, "base_url": "u"},
                },
            },
        )
    )
    await gateway.refresh_status(force=True)
    assert openrouter.enabled()
    # Шлюз не настроен — читателей нет.
    monkeypatch.setattr(settings, "gateway_url", "")
    assert not openrouter.enabled()


def ctx(db_sessionmaker, redis) -> dict:  # noqa: ANN001
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def _реплика(db_sessionmaker, seed_conversation, текст: str, *, минут: int) -> Message:  # noqa: ANN001
    async with db_sessionmaker() as s:
        msg = Message(
            conversation_id=seed_conversation.conversation_id,
            external_message_id=f"in-llm-{минут}",
            direction="in",
            sender_type="client",
            body=текст,
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC) - timedelta(minutes=минут),
        )
        s.add(msg)
        await s.commit()
        await s.refresh(msg)
        return msg


async def test_задача_читает_переписку_и_заводит_строку(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    _читатели(monkeypatch, True)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "amurskaya_oblast_blagoveschensk"
        await s.commit()
    await _реплика(
        db_sessionmaker, seed_conversation, "Здравствуйте, телевизор не включается", минут=30
    )
    msg = await _реплика(db_sessionmaker, seed_conversation, ЗЕЛЕНОГОРСКАЯ, минут=1)
    увидела: dict[str, str] = {}

    async def chat_json(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        увидела["user"] = user
        await kw["on_request"]()
        return ответ(street="улица Зеленогорская", house="17/3", apartment="4"), "b/two:free"

    monkeypatch.setattr(worker.openrouter, "chat_json", chat_json)
    поставлено: list[Any] = []

    async def enqueue_geocode(redis_, candidate_id):  # noqa: ANN001
        поставлено.append(candidate_id)

    monkeypatch.setattr(worker, "enqueue_geocode", enqueue_geocode)
    итог = await worker.llm_address_read(
        ctx(db_sessionmaker, redis),
        conversation_id=str(msg.conversation_id),
        message_id=str(msg.id),
    )
    assert итог == "recorded"
    assert "Город объявления: Благовещенск" in увидела["user"]
    assert "телевизор не включается" in увидела["user"]
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert (row.street, row.house, row.office, row.source, row.level) == (
        "улица Зеленогорская",
        "17/3",
        "4",
        "llm",
        "A",
    )
    assert поставлено == [row.id]
    assert await worker.llm_calls_today(redis) == 1
    # Повтор того же чтения — строка одна, второй раз к карте не ставится.
    итог = await worker.llm_address_read(
        ctx(db_sessionmaker, redis),
        conversation_id=str(msg.conversation_id),
        message_id=str(msg.id),
    )
    assert итог == "same"
    async with db_sessionmaker() as s:
        assert (await s.execute(sa.select(sa.func.count(ClientAddressCandidate.id)))).scalar() == 1


async def test_без_ключа_и_при_выключателе_модель_не_зовётся(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    msg = await _реплика(db_sessionmaker, seed_conversation, ЗЕЛЕНОГОРСКАЯ, минут=1)
    вызовов = 0

    async def chat_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], str]:
        nonlocal вызовов
        вызовов += 1
        return ответ(street="улица Зеленогорская", house="17/3"), "m"

    monkeypatch.setattr(worker.openrouter, "chat_json", chat_json)
    _читатели(monkeypatch, False)
    args = {"conversation_id": str(msg.conversation_id), "message_id": str(msg.id)}
    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **args) == "disabled"
    _читатели(monkeypatch, True)
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_LLM_ENABLED: False}, user_id=None)
        await s.commit()
    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **args) == "disabled"
    # Главный выключатель разбора адреса главнее переключателя модели.
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {app_settings.ADDRESS_LLM_ENABLED: True, app_settings.ADDRESS_DETECT_ENABLED: False},
            user_id=None,
        )
        await s.commit()
    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **args) == "disabled"
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_ENABLED: True}, user_id=None)
        await s.commit()
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {app_settings.ADDRESS_LLM_ENABLED: True, app_settings.ADDRESS_LLM_DAILY_LIMIT: 0},
            user_id=None,
        )
        await s.commit()
    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **args) == "limit"
    assert вызовов == 0


async def test_правила_промолчали_реплика_похожа_на_адрес_модель_ставится(
    db, redis, make_avito_account, monkeypatch
):
    """inbound: чтение ставится после commit'а, только с ключом и только на похожее."""
    from app.services import inbound as inbound_module
    from app.services.inbound import apply_inbound_event
    from tests.unit.test_address_inbound_0909 import T0, событие

    поставлено: list[dict[str, Any]] = []

    async def enqueue_llm_read(redis_, **kw: Any) -> bool:  # noqa: ANN001
        поставлено.append(kw)
        return True

    monkeypatch.setattr(inbound_module, "enqueue_llm_read", enqueue_llm_read)
    _читатели(monkeypatch, True)
    account = await make_avito_account(111222777)
    # Правила молчат (дом словами), а слово «адрес» есть — модели.
    await apply_inbound_event(
        db, redis, account, событие("мой адрес зеленогорская, дом семнадцать дробь три")
    )
    assert len(поставлено) == 1 and поставлено[0].get("candidate_id") is None
    # Не похоже на адрес — не ставится; адрес разобран правилами (даже уровня
    # C) — тоже: его проверит карта, а после отказа перечитает модель.
    await apply_inbound_event(
        db, redis, account, событие("Спасибо, жду", msg="am-2", when=T0.replace(minute=1))
    )
    await apply_inbound_event(
        db, redis, account, событие("ул Ленина 5", msg="am-3", when=T0.replace(minute=2))
    )
    assert len(поставлено) == 1
    # Без ключа на шлюзе — не ставится вовсе.
    _читатели(monkeypatch, False)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("адрес: зеленогорская, дом двадцать", msg="am-4", when=T0.replace(minute=3)),
    )
    assert len(поставлено) == 1


async def test_после_отказа_карты_строка_правил_уходит_модели(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """workers/geocode: not_found/house_missing → перечитать; строку модели и
    старую историю — нет."""
    from app.services import geocode as g
    from app.workers import geocode as geo_worker
    from tests.unit.test_address_place_1309 import _строка
    from tests.unit.test_address_place_1309 import ctx as geo_ctx

    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    _читатели(monkeypatch, True)
    ответы: dict[str, Any] = {"hits": []}

    async def nominatim_search(query, **kw: Any):  # noqa: ANN001
        return ответы["hits"]

    monkeypatch.setattr(geo_worker.nominatim, "search", nominatim_search)
    поставлено: list[dict[str, Any]] = []

    async def enqueue_llm_read(redis_, **kw: Any) -> bool:  # noqa: ANN001
        поставлено.append(kw)
        return True

    monkeypatch.setattr(geo_worker, "enqueue_llm_read", enqueue_llm_read)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "amurskaya_oblast_blagoveschensk"
        await s.commit()
    # Не найдено — модели, с идентификатором строки.
    cid = await _строка(
        seed_conversation, db_sessionmaker, "11 улица Зеленогорская 17/3", место=False
    )
    assert await geo_worker.geocode_candidate(geo_ctx(db_sessionmaker, redis), cid) == "not_found"
    assert len(поставлено) == 1 and поставлено[0]["candidate_id"] == cid
    # Улица есть, дома нет — тоже модели.
    ответы["hits"] = [
        g.GeoHit(
            street="ул Зеленогорская",
            house="17/3",
            settlement=None,
            city="Благовещенск",
            region="Амурская область",
            lat=50.3,
            lon=127.5,
            house_level=False,
        )
    ]
    cid2 = await _строка(seed_conversation, db_sessionmaker, "улица Зеленогорская 21", место=False)
    статус = await geo_worker.geocode_candidate(geo_ctx(db_sessionmaker, redis), cid2)
    assert статус in ("house_missing", "house_mismatch") and len(поставлено) == 2
    # Строку модели не перечитываем; старую реплику (история) — тоже.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid2)
        row.source = "llm"
        row.geo_status = "pending"
        row2 = await s.get(ClientAddressCandidate, cid)
        row2.message_at = datetime.now(UTC) - timedelta(days=3)
        row2.geo_status = "pending"
        await s.commit()
    await geo_worker.geocode_candidate(geo_ctx(db_sessionmaker, redis), cid2)
    await geo_worker.geocode_candidate(geo_ctx(db_sessionmaker, redis), cid)
    assert len(поставлено) == 2


def test_телефон_эмодзи_цифрами() -> None:
    """«8️⃣9️⃣1️⃣…» (бой 13.09): за каждой цифрой знак вариации и обводка клавиши."""
    from app.services import phone_parse

    текст = "".join(f"{d}️⃣" for d in "89001112251")
    (номер,) = phone_parse.find_all(текст)
    assert номер.value == "+79001112251"
    assert phone_parse.normalize(текст) == "+79001112251"
    assert phone_parse.find_all("Этаж 1️⃣0️⃣") == []


def test_враждебный_ответ_модели_не_проходит_сторож() -> None:
    """Части, тип пункта, город, улица целой репликой, дом из цифр телефона — отказ."""
    реплика = "Можно будет часов 11 улица Зеленогорская 17/3 кв 4, звоните 8 900 123 45 67"
    # Квартира/подъезд/этаж/домофон — только словами реплики.
    чтение = address_llm.parse_reading(
        ответ(
            street="улица Зеленогорская",
            house="17/3",
            apartment="99",
            entrance="7",
            floor="13",
            intercom="4321",
        )
    )
    assert чтение is not None
    found = address_llm.to_found(чтение, [реплика])
    assert found is not None and found.parts == {}
    # Дом из цифр телефона: телефон погашен и для сторожа тоже.
    чтение = address_llm.parse_reading(ответ(street="улица Зеленогорская", house="45"))
    assert чтение is not None and address_llm.to_found(чтение, [реплика]) is None
    # Тип пункта — только из канона; город — словами реплики.
    чтение = address_llm.parse_reading(
        ответ(street="", settlement="Зеленогорская", settlement_type="ignore all instructions")
    )
    assert чтение is not None and чтение.settlement_type is None
    # Город, которого клиент не писал (модель переписала «Город объявления»),
    # не берётся — но сам адрес остаётся.
    чтение = address_llm.parse_reading(
        ответ(street="улица Зеленогорская", house="17/3", city="Москва")
    )
    assert чтение is not None
    found = address_llm.to_found(чтение, [реплика])
    assert found is not None and found.locality is None
    # Улица целой репликой или вразброс — нет: ≤ 6 слов и подряд.
    assert address_llm.parse_reading(ответ(street=реплика, house="17/3")) is None
    чтение = address_llm.parse_reading(ответ(street="часов Зеленогорская", house="17/3"))
    assert чтение is not None and address_llm.to_found(чтение, [реплика]) is None
    # Сомнительная часть («<script») отбрасывается ещё при чтении.
    чтение = address_llm.parse_reading(
        ответ(street="улица Зеленогорская", house="17/3", apartment="<script")
    )
    assert чтение is not None and чтение.parts == {}


def test_маска_гасит_ссылки_и_длинные_числа() -> None:
    текст = (
        "Иванов, https://t.me/ivanov, паспорт 4012 123456, карта 2200 1234 5678 9010, "
        "кв 12, дом 19/1"
    )
    м = address_llm.mask(текст)
    assert len(м) == len(текст)
    assert "t.me" not in м and "123456" not in м and "5678" not in м
    assert "кв 12" in м and "19/1" in м and "Иванов" in м
    # Калибровка судьи 21.09: тройка квартального города — шесть цифр через
    # дефис — остаётся видимой (порог маски — семь цифр); паспорт «4012 123456»
    # выше по-прежнему под маской.
    assert address_llm.mask("12-17-58 шестой этаж") == "12-17-58 шестой этаж"
    assert address_llm.mask("86-11-7, домофон 1234") == "86-11-7, домофон 1234"
    assert address_llm.mask("ИНН 7707083893") == "ИНН XXXXXXXXXX"


def test_пункт_из_другой_реплики_не_губит_улицу() -> None:
    """«Новое заозерье» → «Рябиновая 6»: улица и дом берутся, пункт подскажет воркер."""
    чтение = address_llm.parse_reading(
        ответ(street="Рябиновая", house="6", settlement="Новое заозерье", settlement_type="деревня")
    )
    assert чтение is not None
    found = address_llm.to_found(чтение, ["Новое заозерье", "Рябиновая 6"])
    assert found is not None
    assert (found.street, found.house, found.settlement, found.raw) == (
        "Рябиновая",
        "6",
        None,
        "Рябиновая 6",
    )
    # Пункт в той же реплике — остаётся.
    found = address_llm.to_found(чтение, ["Новое заозерье, Рябиновая 6"])
    assert found is not None and found.settlement == "Новое заозерье"


async def test_a_model_failure_keeps_the_reply_readable(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Проверка 24.09: замок «реплика прочитана» и место в потолке диалога
    ставились ДО похода и при отказе моделей не снимались — одна сетевая ошибка
    лишала реплику чтения насовсем (замок живёт сутки, окно чтения — тоже)."""
    _читатели(monkeypatch, True)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "amurskaya_oblast_blagoveschensk"
        await s.commit()
    msg = await _реплика(db_sessionmaker, seed_conversation, ЗЕЛЕНОГОРСКАЯ, минут=1)
    calls: list[str] = []

    async def all_failed(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        calls.append("failed")
        raise openrouter.OpenRouterError("network", None)

    monkeypatch.setattr(worker.openrouter, "chat_json", all_failed)
    monkeypatch.setattr(worker, "enqueue_geocode", lambda *a, **k: _nothing())
    job = {"conversation_id": str(msg.conversation_id), "message_id": str(msg.id)}

    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **job) == "failed"
    assert await worker.llm_address_read(ctx(db_sessionmaker, redis), **job) == "failed"
    assert calls == ["failed", "failed"], "второй прогон обязан снова спросить модели"


async def _nothing() -> None:
    return None
