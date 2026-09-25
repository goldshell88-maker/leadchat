"""Телефоны по случаям владельца 25.09: слова внутри номера, мобильный вплотную
за квартирой, номер по кускам в соседних репликах, подчёркивание внутри номера.

СЛУЧАЙ D — «Восемь 900 111 двадцать два 45»: первая цифра и середина номера
продиктованы словами, как говорят вслух. Разбор знал только хвост словом за
почти целой цепочкой («+790011122 сорок», 18.09): цепочка «900 111» — шесть
цифр, слова стоят МЕЖДУ группами, восьмёрка — словом впереди.

СЛУЧАЙ F — «Садовая 12, кв1 9001112246»: номер квартиры вплотную к «кв», за ним
через пробел десятизначный мобильный. Цепочку «1 9001112246» правило 1
(«приклеено к слову») отвергало ЦЕЛИКОМ — вместе с телефоном, стоящим
отдельно; а правило хвоста цепочек ровно в одиннадцать цифр не смотрело, и
«кв 1 9001112246» с пробелом тоже молчало.

СЛУЧАЙ O — «8900» → «111» → «2247»: номер тремя репликами подряд. Разбор
читает одну реплику, а в каждой — обрывок короче номера.

СЛУЧАЙ R — «номер900-111_22 48»: подчёркивание вместо дефиса (на раскладке
символов они рядом) рвало цепочку на «900-111» и «22 48» — шесть и четыре
цифры.

ДИВЕРСИИ (каждая обязана краснеть): убрать слово-восьмёрку впереди → «Восемь
900 111 двадцать два 45» молчит; принять десять цифр со словами без явной 7/8
→ «900 111 двадцать два 45» станет номером; не смотреть, что стоит перед
словом-восьмёркой → «сорок восемь 900…» даст номер; вернуть `<= 11` в
`_phone_at_the_tail` → «кв 1 9001112246» молчит; снять сторож плюса → «+91
90011 12246» даст +7 900…; отвергать приклеенную цепочку целиком → «кв1
9001112246» молчит; не требовать мобильного за приклеенным → городской
«кв1 8495…» станет номером; убрать подчёркивание из разделителей → случай R
молчит; вернуть `continue` вместо `break` в правиле хвоста → длинная цепочка
разбирается секундами; не рвать куски на реплике оператора → склейка через
его вопрос.

НОМЕРА ЗДЕСЬ ВЫМЫШЛЕННЫЕ, серия +7 900 111-22-4x. У владельца стояли номера
живых клиентов, в тесты они не попадают — форма записи сохранена, цифр нет;
:func:`test_в_файле_только_вымышленные_номера` это сторожит.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Client, ClientPhoneCandidate, Conversation, Message
from app.services import inbound as inbound_svc
from app.services import phone_parse
from app.services.inbound import apply_inbound_event

AVITO_USER_ID = 111222925
T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


# --- случай D: слова внутри номера ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        pytest.param(
            "Восемь 900 111 двадцать два 45", "Восемь 900 111 двадцать два 45", id="как-в-бою"
        ),
        pytest.param(
            "Адрес 12-3-45. Восемь 900 111 двадцать два 45 Иван",
            "Восемь 900 111 двадцать два 45",
            id="после-адреса-перед-именем",
        ),
        pytest.param(
            "Семь 900 111 двадцать два сорок пять",
            "Семь 900 111 двадцать два сорок пять",
            id="семёрка-словом-хвост-словами",
        ),
        pytest.param(
            "8 900 111 двадцать два 45", "8 900 111 двадцать два 45", id="восьмёрка-цифрой"
        ),
        pytest.param(
            "+7 900 111 двадцать два 45, звоните", "+7 900 111 двадцать два 45", id="запятая-после"
        ),
        # Короткое число со словом за номером — этаж, номер оно не продолжает.
        pytest.param(
            "8 900 111 двадцать два 45. 3 этаж", "8 900 111 двадцать два 45", id="этаж-после-точки"
        ),
    ],
)
def test_слова_внутри_номера(text: str, raw: str) -> None:
    """Группы цифр и числа словом вперемешку — один номер; `raw` — со словами."""
    (found,) = phone_parse.find_all(text)
    assert found.value == "+79001112245"
    assert found.raw == raw
    assert text[found.start : found.end] == raw


@pytest.mark.parametrize(
    "text",
    [
        # Десять цифр со словами и без явной 7/8 — тот же отказ, что у «900111224
        # ноль» (18.09): дописать +7 значило бы угадать, что потеряна семёрка.
        pytest.param("900 111 двадцать два 45", id="десять-цифр-без-восьмёрки"),
        pytest.param("8 900 111 двадцать два 4", id="десять-цифр-с-восьмёрки"),
        # «сорок восемь» — это 48, а не восьмёрка: цепочка слов читается целиком.
        pytest.param("сорок восемь 900 111 двадцать два 45", id="сорок-восемь"),
        # Цифра перед словом-восьмёркой: где начинается номер, не знает никто.
        pytest.param("5 восемь 900 111 двадцать два 45", id="цифра-перед-восьмёркой"),
        # Точка кончила фразу: «Квартира восемь. 900…» — восьмёрка не код страны.
        pytest.param("Квартира восемь. 900 111 двадцать два 45", id="точка-после-восьмёрки"),
        pytest.param("Восемь 900 111 двадцать два 45 12", id="двенадцать-цифр"),
        pytest.param("Восемь 900 111 двадцать два 45, 12", id="ещё-число-через-запятую"),
        pytest.param("Восемь 900 111 двадцать два 45 рублей", id="единица-счёта"),
        pytest.param("Восемь 900 111 двадцать два 45abc", id="приклеено-к-слову"),
        pytest.param("8 900 111 две тысячи 45", id="множитель"),
        pytest.param("8 495 111 двадцать два 45", id="городской"),
        pytest.param("Нужно 2 кондиционера, 9 000 рублей, сорок пять минут", id="количества"),
    ],
)
def test_слова_внутри_номера_не_придумывают_номеров(text: str) -> None:
    """«Лучше пропустить, чем придумать» действует и на номер, продиктованный словами."""
    assert phone_parse.find_all(text) == []


# --- случай F: мобильный вплотную за номером квартиры --------------------------


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        pytest.param("Садовая 12, кв1 9001112246", "9001112246", id="как-в-бою"),
        pytest.param("Садовая 12, кв 1 9001112246", "9001112246", id="квартира-через-пробел"),
        pytest.param("Садовая 12 кв1 89001112246", "89001112246", id="одиннадцать-цифр"),
        pytest.param("д5 кв1 9001112246 Иван", "9001112246", id="дом-и-квартира-вплотную"),
        pytest.param("1) 9001112246", "9001112246", id="нумерация-списка"),
    ],
)
def test_мобильный_за_номером_квартиры(text: str, raw: str) -> None:
    """К слову приклеен номер квартиры, а телефон стоит отдельно — это телефон."""
    (found,) = phone_parse.find_all(text)
    assert found.value == "+79001112246"
    assert found.raw == raw
    assert text[found.start : found.end] == raw


@pytest.mark.parametrize(
    "text",
    [
        # Плюс открывает код страны: «+1 …» и «+91 …» — чужие номера целиком, и
        # их первая группа — не «контекст», который можно отрезать.
        pytest.param("+1 900 111 2246", id="код-страны-однозначный"),
        pytest.param("+91 90011 12246", id="код-страны-двузначный"),
        pytest.param("кв1 900111224", id="девять-цифр"),
        pytest.param("кв1 4951112246", id="десять-не-с-девятки"),
        # За приклеенным числом сигнал слабее: только мобильный.
        pytest.param("кв1 84951112246", id="городской-за-приклеенным"),
        pytest.param("кв1 9001112246abc", id="латиница-справа"),
        # Знак номера и латиница слева по-прежнему отвергают цепочку целиком.
        pytest.param("№1 9001112246", id="знак-номера"),
        pytest.param("Q70 9001112246", id="латиница-слева-модель"),
        # Слева больше четырёх цифр — не «контекст и номер», а неизвестно что.
        pytest.param("кв12345 9001112246", id="длинное-число-слева"),
        pytest.param("кв1 900-111-22-46-77", id="тринадцать-цифр"),
        # Прежние контрпримеры правила 1.
        pytest.param("тел8900111224", id="буквы-слева-не-целый"),
        pytest.param("Код входной двери в дом: 37ключ2580", id="код-домофона"),
    ],
)
def test_за_приклеенным_числом_не_придумывает_номеров(text: str) -> None:
    assert phone_parse.find_all(text) == []


# --- случай R: подчёркивание внутри номера ---------------------------------------


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        pytest.param("Ок, звоните на номер900-111_22 48, Иван.", "900-111_22 48", id="как-в-бою"),
        pytest.param("звоните 8 900 111_22_48", "8 900 111_22_48", id="подчёркивания-везде"),
    ],
)
def test_подчёркивание_внутри_номера(text: str, raw: str) -> None:
    (found,) = phone_parse.find_all(text)
    assert found.value == "+79001112248"
    assert found.raw == raw


def test_поле_ввода_терпит_подчёркивание() -> None:
    """Ручной ввод и разбор переписки — один список разделителей."""
    assert phone_parse.normalize("8_900_111_22_48") == "+79001112248"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("photo_2026_09_25_12_34_56.jpg", id="имя-файла"),
        pytest.param("IMG_20260925_123456", id="снимок"),
        pytest.param("номер900-111_22 4", id="девять-цифр-у-слова"),
    ],
)
def test_подчёркивание_не_придумывает_номеров(text: str) -> None:
    assert phone_parse.find_all(text) == []


@pytest.mark.parametrize("sep", [" ", "_"], ids=["пробелы", "подчёркивания"])
def test_длинная_цепочка_разбирается_линейно(sep: str) -> None:
    """Сообщение Авито не ограничено по длине, а разбор зовут из потока входящих.

    Правило хвоста пересчитывало цифры префикса на каждой позиции цепочки: «12 12
    12 …» в 60 тысяч знаков — шестнадцать секунд на одно входящее, и
    подчёркивание в разделителях делает такой цепочкой ещё и «12_12_12…». Порог
    щедрый: исправленный разбор укладывается в миллисекунды, сломанный — в секунды.
    """
    text = sep.join(["12"] * 20000)
    started = time.perf_counter()
    assert phone_parse.find_all(text) == []
    assert time.perf_counter() - started < 1.0


# --- случай O: номер по кускам в соседних репликах --------------------------------


@pytest.mark.parametrize(
    ("parts", "raw"),
    [
        pytest.param(["8900", "111", "2247"], "8900; 111; 2247", id="как-в-бою"),
        pytest.param(
            ["Здравствуйте, запишите", "8900", "111", "2247"],
            "8900; 111; 2247",
            id="речь-до-кусков",
        ),
        pytest.param(["+7 900", "111-22-47"], "+7 900; 111-22-47", id="два-куска"),
        pytest.param(["900 111", "22 47"], "900 111; 22 47", id="десять-цифр"),
        pytest.param([" 8900 ", "111", "2247\n"], "8900; 111; 2247", id="пробелы-по-краям"),
    ],
)
def test_номер_по_кускам(parts: list[str | None], raw: str) -> None:
    """Куски — старые первыми, последний — текущая реплика; `raw` — через «; »."""
    found = phone_parse.from_fragments(parts)
    assert found is not None
    assert found.value == "+79001112247"
    assert found.raw == raw
    assert (found.start, found.end) == (0, len(raw))


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param(["8900", "111", "224"], id="десять-цифр-с-восьмёрки"),
        # Лишняя цифра следом за целым номером — не новый номер.
        pytest.param(["8900", "111", "22", "47", "5"], id="цифра-после-номера"),
        pytest.param(["12", "900 111", "22 4"], id="квартира-и-обрывок"),
        pytest.param(["8900", "Лесная 5", "111 2247"], id="речь-между-кусками"),
        pytest.param(["8900", None, "111 2247"], id="пустая-реплика-между"),
        pytest.param(["8900", "111", "2247 Иван"], id="текущая-не-обрывок"),
        pytest.param(["89001112247", "5"], id="целый-номер-не-обрывок"),
        pytest.param(["+7 900", "+111", "2247"], id="плюс-в-середине"),
        pytest.param(["8495", "111", "2247"], id="городской"),
        pytest.param(["2247"], id="один-кусок"),
        pytest.param([], id="пусто"),
    ],
)
def test_куски_не_придумывают_номеров(parts: list[str | None]) -> None:
    assert phone_parse.from_fragments(parts) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8900", True),
        (" 111-22 ", True),
        ("+7 900", True),
        ("900111224", True),
        ("9001112247", False),  # целый номер — не обрывок
        ("кв 5", False),
        ("", False),
        (None, False),
    ],
)
def test_что_считается_обрывком(text: str | None, expected: bool) -> None:
    assert phone_parse.is_fragment(text) is expected


# --- случай O на живом пути и на догоне ------------------------------------------


def событие(текст: str, *, msg: str, when: datetime) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-2509",
        external_message_id=msg,
        author_id=999025,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Клиент",
        item_title="Ремонт холодильников",
        item_url="https://www.avito.ru/tambov/predlozheniya_uslug/remont_123456789",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


async def _кандидаты(db_sessionmaker) -> list[ClientPhoneCandidate]:
    async with db_sessionmaker() as s:
        return list((await s.execute(sa.select(ClientPhoneCandidate))).scalars())


async def _сообщение(db_sessionmaker, внешний: str) -> Message:
    async with db_sessionmaker() as s:
        return (
            await s.execute(sa.select(Message).where(Message.external_message_id == внешний))
        ).scalar_one()


async def _исходящее(db_sessionmaker, текст: str, when: datetime) -> None:
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"out-{when.timestamp()}",
                direction="out",
                sender_type="user",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def test_номер_тремя_репликами_ложится_в_карточку(
    db, redis, account, db_sessionmaker
) -> None:
    """«8900» → «111» → «2247» → место: один кандидат, с последнего куска."""
    for i, текст in enumerate(["8900", "111", "2247", "Посёлок Лесной"]):
        await apply_inbound_event(
            db, redis, account, событие(текст, msg=f"m{i}", when=T0 + timedelta(seconds=20 * i))
        )
    (кандидат,) = await _кандидаты(db_sessionmaker)
    assert кандидат.phone == "+79001112247"
    assert кандидат.raw == "8900; 111; 2247"
    assert кандидат.message_id == (await _сообщение(db_sessionmaker, "m2")).id


async def test_реплика_оператора_между_кусками_рвёт_номер(
    db, redis, account, db_sessionmaker
) -> None:
    """Ответ оператора между кусками — это уже разговор, а не диктовка номера."""
    await apply_inbound_event(db, redis, account, событие("8900", msg="m0", when=T0))
    await _исходящее(db_sessionmaker, "Слушаю вас", T0 + timedelta(seconds=10))
    await apply_inbound_event(
        db, redis, account, событие("111", msg="m1", when=T0 + timedelta(seconds=20))
    )
    await apply_inbound_event(
        db, redis, account, событие("2247", msg="m2", when=T0 + timedelta(seconds=40))
    )
    assert await _кандидаты(db_sessionmaker) == []


async def test_куски_через_полчаса_не_склеиваются(db, redis, account, db_sessionmaker) -> None:
    """Первый кусок за пределами окна — номер не собирается из разговора за час."""
    await apply_inbound_event(db, redis, account, событие("8900", msg="m0", when=T0))
    await apply_inbound_event(
        db, redis, account, событие("111", msg="m1", when=T0 + timedelta(minutes=30))
    )
    await apply_inbound_event(
        db, redis, account, событие("2247", msg="m2", when=T0 + timedelta(minutes=31))
    )
    assert await _кандидаты(db_sessionmaker) == []


async def test_догон_истории_собирает_номер_по_кускам(db, redis, account, db_sessionmaker) -> None:
    """Догон (`replay_card_extraction`) идёт тем же путём, что живой приём."""
    await apply_inbound_event(db, redis, account, событие("здравствуйте", msg="m0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        client = await s.get(Client, conv.client_id)
        assert client is not None
        куски = [
            Message(
                conversation_id=conv.id,
                external_message_id=f"h{i}",
                direction="in",
                sender_type="client",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=1, seconds=15 * i),
            )
            for i, текст in enumerate(["8900", "111", "2247"])
        ]
        s.add_all(куски)
        await s.flush()
        причины = [
            (await inbound_svc.replay_card_extraction(s, conv, client, m, now=m.created_at))[0]
            for m in куски
        ]
        await s.commit()
    assert причины == [None, None, "phone_suggested"]
    (кандидат,) = await _кандидаты(db_sessionmaker)
    assert (кандидат.phone, кандидат.raw) == ("+79001112247", "8900; 111; 2247")


# --- сторож: в этом файле нет номеров живых людей ----------------------------------

#: `\uXXXX`, `\t`, `\n` в исходнике — текст с обратной косой, а не символ.
_ЭКРАНИРОВАНИЕ = re.compile(r"\\(?:u[0-9a-fA-F]{4}|[tnr])")


def test_в_файле_только_вымышленные_номера() -> None:
    """Всё, что разбор находит в тексте этого файла, — из вымышленной серии +7 900
    111-22-4x. Диверсия: любой номер другой серии в любом тесте выше."""
    исходник = Path(__file__).read_text(encoding="utf-8")
    текст = _ЭКРАНИРОВАНИЕ.sub(lambda m: "\n" if m.group(0) in ("\\n", "\\r") else " ", исходник)
    чужие = sorted(
        {f.raw for f in phone_parse.find_all(текст) if not f.value.startswith("+790011122")}
    )
    assert чужие == []
