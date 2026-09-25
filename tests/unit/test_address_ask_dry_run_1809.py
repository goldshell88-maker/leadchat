"""Сухой прогон `address-ask-dry-run` — ворота включения вопроса об адресе (18.09).

Реплика клиенту от имени компании по счётчику букв не должна уйти, пока порог,
стоп-список и задержка не посмотрены на боевом корпусе. Прогон зовёт ТЕ ЖЕ
функции `services/address_ask.py`, что решают в бою, а не свой SQL; тел в
выводе нет, кроме выборки для глаз владельца.

Сид из трёх диалогов: описал и никто не ответил 20 минут → `would_send` в
корзине «никогда»; «Сколько стоит?» → `not_described`; оператор ответил через
200 с → `answered_in_time` при задержке 600 и `would_send`+«пересечение» при 180.

Второй сид — замки ревью 19.09 ТЕМ ЖЕ предикатом, что в задаче
(`address_ask.candidate_lock`/`card_lock`): помеченный клиент → `client_blocked`
(и печатается, даже с нулём); строка `not_found` без степени → `would_send`
(контракт п.2: вопрос клиенту); строка, которую карта ещё проверяет →
`geo_pending` (на прожитой переписке — замок, повтора у прогона нет); строка со
степенью → `candidate_exists`. Выборка для глаз — только маской: телефон
крестиками, имя клиента `[имя]`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import run_address_ask_dry_run
from app.models import Client, ClientAddressCandidate, Conversation, Message

pytestmark = pytest.mark.anyio

ОПИСАЛ = "Здравствуйте! Телевизор не включается, индикатор мигает"
ТОЛЬКО_ЦЕНА = "Сколько стоит?"
С_ТЕЛЕФОНОМ = "Здравствуйте, это Пётр Адресов, стиралка не крутит, мой номер +7 900 123-45-67"


async def _диалог(
    s: AsyncSession,
    account_id: uuid.UUID,
    *,
    external_id: str,
    минут_назад: int,
    имя: str | None = None,
    blocked: bool = False,
) -> tuple[Conversation, datetime]:
    t0 = datetime.now(UTC) - timedelta(minutes=минут_назад)
    клиент = Client(
        channel="avito",
        external_id=external_id,
        name=имя,
        blocked_at=datetime.now(UTC) if blocked else None,
    )
    s.add(клиент)
    await s.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=f"chat-{external_id}",
        account_id=account_id,
        client_id=клиент.id,
        status="new",
        unread_count=1,
        last_message_at=t0,
    )
    s.add(conv)
    await s.flush()
    return conv, t0


def _сообщение(conv: Conversation, *, когда: datetime, body: str, out: bool = False) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        direction="out" if out else "in",
        sender_type="operator" if out else "client",
        body=body,
        attachments=[],
        delivery_status="delivered",
        created_at=когда,
    )


@pytest.fixture
async def корпус(db_sessionmaker, make_avito_account) -> None:
    account = await make_avito_account(111222777)
    async with db_sessionmaker() as s:
        молчание, t0 = await _диалог(s, account.id, external_id="900101", минут_назад=20)
        s.add(_сообщение(молчание, когда=t0, body=ОПИСАЛ))
        цена, t0 = await _диалог(s, account.id, external_id="900102", минут_назад=20)
        s.add(_сообщение(цена, когда=t0, body=ТОЛЬКО_ЦЕНА))
        ответили, t0 = await _диалог(s, account.id, external_id="900103", минут_назад=20)
        s.add(_сообщение(ответили, когда=t0, body=ОПИСАЛ))
        s.add(
            _сообщение(
                ответили, когда=t0 + timedelta(seconds=200), body="Сейчас посмотрю", out=True
            )
        )
        await s.commit()


def _строка(conv: Conversation, *, когда: datetime, **поля: object) -> ClientAddressCandidate:
    значения: dict[str, object] = {
        "value": "ул Ленина, 5",
        "street": "ул Ленина",
        "house": "5",
        "raw": "ул Ленина 5",
        "level": "A",
        "status": "pending",
        "kind": "house",
        "geo_status": "pending",
        "detected_at": когда,
    }
    значения.update(поля)
    return ClientAddressCandidate(client_id=conv.client_id, conversation_id=conv.id, **значения)


@pytest.fixture
async def корпус_замков(db_sessionmaker, make_avito_account) -> None:
    """Пять диалогов, все описали и 20 минут без ответа; различаются карточкой
    и строкой адреса."""
    account = await make_avito_account(111222778)
    async with db_sessionmaker() as s:
        помеченный, t0 = await _диалог(
            s, account.id, external_id="900201", минут_назад=20, blocked=True
        )
        s.add(_сообщение(помеченный, когда=t0, body=ОПИСАЛ))
        отказ, t0 = await _диалог(s, account.id, external_id="900202", минут_назад=20)
        s.add(_сообщение(отказ, когда=t0, body=ОПИСАЛ))
        s.add(_строка(отказ, когда=t0, geo_status="not_found", geo_formatted=None))
        ждёт, t0 = await _диалог(s, account.id, external_id="900203", минут_назад=20)
        s.add(_сообщение(ждёт, когда=t0, body=ОПИСАЛ))
        s.add(_строка(ждёт, когда=t0, geo_status="pending"))
        степень, t0 = await _диалог(s, account.id, external_id="900204", минут_назад=20)
        s.add(_сообщение(степень, когда=t0, body=ОПИСАЛ))
        s.add(_строка(степень, когда=t0, geo_status="exact", geo_lat=55.0, geo_lon=37.0))
        с_телефоном, t0 = await _диалог(
            s, account.id, external_id="900205", минут_назад=20, имя="Пётр Адресов"
        )
        s.add(_сообщение(с_телефоном, когда=t0, body=С_ТЕЛЕФОНОМ))
        await s.commit()


async def test_счётчики_при_задержке_600(корпус, db, capsys):
    итог = await run_address_ask_dry_run(db, days=30, delay=600, min_chars=25, limit=100, sample=0)
    assert итог["conversations"] == 3
    assert итог["answered_in_time"] == 1
    assert итог["locks"] == {"would_send": 1, "not_described": 1}
    assert итог["buckets"] == {"never": 1}
    вывод = capsys.readouterr().out
    assert "would_send" in вывод and "not_described" in вывод
    # Тел в выводе нет — только счётчики (sample=0).
    assert ОПИСАЛ not in вывод and ТОЛЬКО_ЦЕНА not in вывод


async def test_при_задержке_180_ответ_через_200_с_становится_пересечением(корпус, db, capsys):
    итог = await run_address_ask_dry_run(db, days=30, delay=180, min_chars=25, limit=100, sample=10)
    assert итог["answered_in_time"] == 0
    assert итог["locks"]["would_send"] == 2
    assert итог["buckets"] == {"never": 1, "overlap_7min": 1}
    assert итог["overlap_180_600"] == 1
    # Выборка — для глаз владельца, в терминал.
    assert ОПИСАЛ[:120] in capsys.readouterr().out


async def test_порог_ноль_снимает_замок_описания(корпус, db):
    итог = await run_address_ask_dry_run(db, days=30, delay=600, min_chars=0, limit=100, sample=0)
    assert "not_described" not in итог["locks"]
    assert итог["locks"]["would_send"] == 2


async def test_замки_ревью_тем_же_предикатом_что_в_задаче(корпус_замков, db, capsys):
    """ДИВЕРСИИ: вернуть `latest_address_candidate is not None` — `would_send`
    станет 1, `candidate_exists` 3; убрать чёрный список из `card_lock` —
    `client_blocked` пропадёт, `would_send` станет 3."""
    итог = await run_address_ask_dry_run(db, days=30, delay=600, min_chars=25, limit=100, sample=10)
    assert итог["conversations"] == 5
    assert итог["locks"] == {
        "would_send": 2,  # отказ карты без степени + диалог с телефоном
        "client_blocked": 1,
        "geo_pending": 1,
        "candidate_exists": 1,
    }
    вывод = capsys.readouterr().out
    assert "  client_blocked: 1" in вывод and "  geo_pending: 1" in вывод
    # Замки прогона печатаются и с нулём — владелец видит число, а не ищет его.
    assert "  card_has_address: 0" in вывод and "  asked_in_feed: 0" in вывод
    # Выборка — маской: телефон крестиками, имя клиента скрыто, суть реплики видна.
    assert "стиралка не крутит" in вывод
    assert "123-45-67" not in вывод and "900 123" not in вывод
    assert "Пётр" not in вывод and "Адресов" not in вывод and "[имя]" in вывод
