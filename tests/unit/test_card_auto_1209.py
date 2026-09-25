"""Автоматика карточки клиента (12.09): номера сами, двойники сами, обратимо.

ЧТО ОХРАНЯЕТСЯ:
* все номера сообщения попадают в карточку: первый — в пустой основной,
  остальные — дополнительными, с подсказкой и без вопроса;
* заполненный основной автоматика НЕ МЕНЯЕТ никогда — похожий номер только
  помечается `near_primary`;
* наши номера и 8-800 не пишутся вовсе — и на пустой карточке тоже;
* «Не его номер» снимает принятый дополнительный, основной так не снять;
* двойники объединяются сами только при ВСЕХ условиях правила, каждый отказ
  именован; тень пишет строку и не объединяет; автостоп по разъединениям;
* «Разъединить» возвращает и диалоги, заведённые после объединения, и
  запоминает пару; заявка объединённой карточки не встаёт (`lead_phone` 3′).

ДИВЕРСИИ (каждая обязана краснеть): снять условный `UPDATE … WHERE phone IS
NULL` → гонка затирает основной; убрать проверку `names_equal` → «Александра»
склеится с «Александром»; убрать `shared_history` → третья карточка сольётся
после первых двух; убрать veto → разъединённая пара склеится снова; убрать
`origin_client_id` из unmerge → поздний диалог останется у победителя.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
import typer

from app.models import AuditLog, Client, ClientMergeVeto, ClientPhoneCandidate, Conversation
from app.services import app_settings, client_merge, phone_parse
from app.services import clients as clients_svc
from app.services.inbound import apply_inbound_event
from app.workers import client_merge as worker
from tests.unit.conftest import drain_events

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)
A = "+79001112241"
B = "+79001112253"


def событие(
    text: str,
    *,
    author: int = 5001,
    chat: str = "chat-a",
    mid: str | None = None,
    at: datetime = T0,
    name: str = "Иван Петров",
    account_user: int = 111222333,
) -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat,
        external_message_id=mid or f"m-{uuid.uuid4().hex[:8]}",
        author_id=author,
        account_user_id=account_user,
        text=text,
        created_at=at,
        client_name=name,
        item_title="Ремонт",
        item_url="https://avito.ru/orsk/item/1",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(111222333)


@pytest.fixture
async def account2(make_avito_account):
    return await make_avito_account(222333444, title="LP-Test-2")


@pytest.fixture
def read(db_sessionmaker):
    async def _read(stmt):
        async with db_sessionmaker() as s:
            return list((await s.execute(stmt)).scalars())

    return _read


async def _настроить(db, **values) -> None:
    patch = {app_settings.PHONE_DETECT_AUTOFILL: True}
    for k, v in values.items():
        patch[getattr(app_settings, k)] = v
    await app_settings.set_many(db, patch, user_id=None)
    await db.commit()


def ctx(db_sessionmaker, redis) -> dict:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def задача(db_sessionmaker, redis, phone: str = A, *, backfill: bool = False) -> str:
    """Как задачу ставит очередь: номер под ключом в Redis, задаче — только ключ."""
    from app.services import merge_queue

    key = merge_queue.phone_key(phone)
    await redis.set(merge_queue.arg_key(key), phone, ex=3600)
    return await worker.merge_twins(ctx(db_sessionmaker, redis), key, backfill=backfill)


# --- номера ------------------------------------------------------------------


async def test_два_номера_в_одном_сообщении_первый_основной_второй_дополнительный(
    db, redis, account, read, db_sessionmaker
):
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(f"мой {A[2:]}, жены {B[2:]}"))
    (card,) = await read(sa.select(Client))
    assert card.phone == A
    rows = await read(sa.select(ClientPhoneCandidate).order_by(ClientPhoneCandidate.phone))
    assert [(r.phone, r.status, r.resolved_by_id, r.hint) for r in rows] == [
        (A, "accepted", None, None),
        (B, "accepted", None, "жены"),
    ]
    журнал = await read(sa.select(AuditLog).order_by(AuditLog.id))
    действия = [(a.action, a.user_id, (a.details or {}).get("auto")) for a in журнал]
    assert ("client.phone_captured", None, None) in действия
    assert ("client.phone_candidate_accepted", None, True) in действия
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, card.id))
    extra = [p for p in view["phones"] if not p["primary"]]
    assert [(p["value"], p["hint"], p["decided_by"], p["near_primary"]) for p in extra] == [
        (B, "жены", "auto", False)
    ]


async def test_наш_и_8_800_не_пишутся_даже_на_пустую_карточку(db, redis, account, read):
    """⚠ ДИВЕРСИЯ: убрать проверку «наш» до выбора основного — основным станет наш."""
    await _настроить(db, PHONE_OWN_NUMBERS="8 900 111 22 59")
    await apply_inbound_event(
        db, redis, account, событие(f"звонил вам на 8 800 555 35 35 и 8 900 111 22 59, мой {A[2:]}")
    )
    (card,) = await read(sa.select(Client))
    assert card.phone == A
    rows = await read(sa.select(ClientPhoneCandidate))
    assert [r.phone for r in rows] == [A], "ни нашего, ни 8-800 в строках быть не должно"


async def test_похожий_номер_не_меняет_основной_а_помечается(
    db, redis, account, read, db_sessionmaker
):
    """⚠ ГЛАВНЫЙ ЗАСЛОН: опечатка ли это или соседняя SIM — из текста не узнать.

    Номер ложится дополнительным с пометкой `near_primary`; основной — как был.
    """
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("правильный номер " + A[2:-1] + "9", at=T0 + timedelta(minutes=3)),
    )
    (card,) = await read(sa.select(Client))
    assert card.phone == A, "автоматика не меняет заполненный основной никогда"
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, card.id))
    extra = [p for p in view["phones"] if not p["primary"]]
    assert [(p["value"][-1], p["near_primary"]) for p in extra] == [("9", True)]


async def test_отклонённое_человеком_не_воскресает_повтором(
    db, redis, account, read, users_by_role
):
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    await apply_inbound_event(
        db, redis, account, событие("ещё " + B[2:], at=T0 + timedelta(minutes=1))
    )
    (строка,) = await read(sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.phone == B))
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, строка.id)
        итог = await clients_svc.resolve_phone_candidate(
            db, row, decision="reject", actor=users_by_role["manager"]
        )
    assert итог["candidate"]["status"] == "rejected"
    await apply_inbound_event(db, redis, account, событие(B[2:], at=T0 + timedelta(minutes=5)))
    (строка,) = await read(sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.phone == B))
    assert строка.status == "rejected"
    журнал = await read(
        sa.select(AuditLog).where(AuditLog.action == "client.phone_candidate_rejected")
    )
    assert журнал[0].details["overrides_auto"] is True


async def test_основной_нельзя_снять_кнопкой_не_его_номер(db, redis, account, read, users_by_role):
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    (строка,) = await read(sa.select(ClientPhoneCandidate))
    from app.core.errors import ApiError

    async with db.begin():
        row = await db.get(ClientPhoneCandidate, строка.id)
        with pytest.raises(ApiError) as exc:
            await clients_svc.resolve_phone_candidate(
                db, row, decision="reject", actor=users_by_role["manager"]
            )
    assert exc.value.code == "is_primary"


async def test_гонка_за_пустой_основной_проигравший_ложится_дополнительным(
    db, redis, account, read, db_sessionmaker
):
    """⚠ ДИВЕРСИЯ: заменить условный UPDATE присваиванием — второй затрёт первого."""
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("здравствуйте"))
    (card,) = await read(sa.select(Client))
    (conv,) = await read(sa.select(Conversation))
    async with db_sessionmaker() as s1, db_sessionmaker() as s2:
        c1 = await s1.get(Client, card.id)
        c2 = await s2.get(Client, card.id)
        assert c1.phone is None and c2.phone is None
        # Первый вебхук успел записать основной и закоммитить.
        await s1.execute(sa.update(Client).where(Client.id == card.id).values(phone=A))
        await s1.commit()
        # Второй вебхук держит УСТАРЕВШИЙ объект (phone=None) и пишет B.
        итог = await clients_svc.absorb_phones(
            s2,
            client=c2,
            conversation_id=conv.id,
            account_id=account.id,
            message_id=None,
            message_at=T0,
            text=B[2:],
            found=phone_parse.find_all(B[2:]),
            now=T0,
            autofill=True,
            own=frozenset(),
        )
        await s2.commit()
    assert (итог.primary_filled, итог.extras_added) == (False, 1)
    (card,) = await read(sa.select(Client))
    assert card.phone == A, "победитель гонки остаётся основным"


async def test_кадр_и_постановка_объединения_после_commit(db, redis, account, monkeypatch):
    """⚠ ДИВЕРСИЯ: перенести `enqueue_merge` внутрь транзакции — краснеет."""
    from app.services import inbound as inbound_mod

    состояния: list[tuple[bool, str]] = []

    async def подсмотреть(redis_, phone, **kw):  # noqa: ANN001
        состояния.append((db.in_transaction(), phone))
        return True

    monkeypatch.setattr(inbound_mod, "enqueue_merge", подсмотреть)
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    assert состояния == [(False, A)]
    # Дополнительный номер объединение не ставит: правило смотрит на основной.
    await apply_inbound_event(db, redis, account, событие(B[2:], at=T0 + timedelta(minutes=1)))
    assert len(состояния) == 1


async def test_на_догрузке_истории_объединение_не_ставится(db, redis, account, monkeypatch):
    from app.services import inbound as inbound_mod

    вызовы: list[str] = []

    async def подсмотреть(redis_, phone, **kw):  # noqa: ANN001
        вызовы.append(phone)
        return True

    monkeypatch.setattr(inbound_mod, "enqueue_merge", подсмотреть)
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]), backfill=True)
    assert вызовы == []


# --- двойники ------------------------------------------------------------------


async def _пара(
    db,
    redis,
    account,
    account2,
    *,
    name2: str = "Иван Петров",
    phone2: str = A,
    at2: datetime | None = None,
    author2: int = 5002,
):
    """Две карточки на двух наших аккаунтах, обе назвали номер в своей переписке."""
    await apply_inbound_event(db, redis, account, событие(A[2:], author=5001, chat="chat-a"))
    await apply_inbound_event(
        db,
        redis,
        account2,
        событие(
            phone2[2:],
            author=author2,
            chat="chat-b",
            name=name2,
            account_user=222333444,
            at=at2 or T0 + timedelta(hours=1),
        ),
    )


async def test_двойники_объединяются_сами_с_журналом_и_кадрами(
    db, redis, account, account2, read, db_sessionmaker
):
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert await задача(db_sessionmaker, redis) == "merged"
    cards = await read(sa.select(Client).order_by(Client.external_id))
    ушедшие = [c for c in cards if c.merged_into_id is not None]
    assert len(ушедшие) == 1
    loser = ушедшие[0]
    winner = next(c for c in cards if c.id == loser.merged_into_id)
    assert winner.external_id == "5001", "победитель — старшая история"
    (row,) = await read(sa.select(AuditLog).where(AuditLog.action == "client.merged"))
    assert row.user_id is None and row.details["auto"] is True
    assert row.details["rule"] == client_merge.RULE
    assert (
        row.details["proof"]["winner"]["account_id"] != row.details["proof"]["loser"]["account_id"]
    )
    кадры = await drain_events(pubsub)
    типы = [(e["type"], e["data"].get("reason")) for e in кадры if e["type"] == "client:updated"]
    assert типы == [("client:updated", "merged_auto")] * 2
    патчи = [e["data"]["patch"]["client"] for e in кадры if e["type"] == "conversation:updated"]
    # Переехавший диалог и собственный диалог победителя: у коллег оба обязаны
    # показать одно имя (проверка 24.09).
    assert патчи == [{"id": str(winner.id), "name": "Иван Петров", "phone": A}] * 2
    # Диалог проигравшего переехал.
    convs = await read(sa.select(Conversation))
    assert {c.client_id for c in convs} == {winner.id}
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, winner.id))
    assert view["merged_from"][0]["auto"] is True
    assert view["merged_from"][0]["confidence"] == "confirmed"
    assert [p["value"] for p in view["phones"]] == [A], "один номер — одна строка"


@pytest.mark.parametrize(
    ("подготовка", "причина"),
    [
        ("names_differ", "names_differ"),
        ("same_account", "same_account"),
        ("group", "group"),
        ("shared_history", "shared_history"),
        ("shared_candidate", "shared_candidate"),
        ("unproven", "unproven"),
        ("gap", "gap_too_wide"),
        ("stub", "stub"),
        ("blocked", "blocked"),
        ("phone_conflict", "phone_conflict"),
        ("cross_account", "cross_account"),
        ("has_children", "has_children"),
        ("no_messages", "no_messages"),
        ("toll_free", "own_or_toll_free"),
    ],
)
async def test_каждое_условие_правила_именованный_отказ(
    db, redis, account, account2, read, db_sessionmaker, подготовка: str, причина: str
):
    """⚠ ДИВЕРСИЯ: убрать любую проверку из `check_twins` — соответствующий случай
    склеится, и тест покраснеет на «skipped:<причина>»."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    if подготовка == "names_differ":
        await _пара(db, redis, account, account2, name2="Александра")
    elif подготовка == "same_account":
        await apply_inbound_event(db, redis, account, событие(A[2:], author=5001, chat="chat-a"))
        await apply_inbound_event(db, redis, account, событие(A[2:], author=5002, chat="chat-b"))
    elif подготовка == "gap":
        await _пара(db, redis, account, account2, at2=T0 + timedelta(days=120))
    elif подготовка == "toll_free":
        # Оба «назвали» 8-800 — карточки с таким основным быть не должно, но
        # правило обязано отказать даже если он туда попал руками.
        await _пара(db, redis, account, account2)
        async with db_sessionmaker() as s:
            await s.execute(sa.update(Client).values(phone="+78005553535"))
            await s.commit()
        assert await задача(db_sessionmaker, redis, "+78005553535") == "skipped:own_or_toll_free"
        return
    else:
        await _пара(db, redis, account, account2)
        async with db_sessionmaker() as s:
            cards = list(
                (await s.execute(sa.select(Client).order_by(Client.external_id))).scalars()
            )
            if подготовка == "group":
                # Больше MAX_GROUP карточек с одним номером — номер конторы.
                for n in range(3):
                    s.add(
                        Client(channel="avito", external_id=f"600{n}", name="Иван Петров", phone=A)
                    )
            elif подготовка == "shared_history":
                # Номер уже уходил в ЧУЖУЮ карточку (не из этой группы).
                чужая = Client(channel="avito", external_id="5009", name="Кто-то")
                s.add(чужая)
                await s.flush()
                s.add(
                    Client(
                        channel="avito",
                        external_id="5003",
                        name="Иван Петров",
                        phone=A,
                        merged_into_id=чужая.id,
                        merged_at=T0,
                    )
                )
            elif подготовка == "shared_candidate":
                третья = Client(channel="avito", external_id="5003", name="Кто-то")
                s.add(третья)
                await s.flush()
                s.add(
                    ClientPhoneCandidate(
                        client_id=третья.id,
                        conversation_id=None,
                        phone=A,
                        raw=A,
                        source="swap",
                        status="accepted",
                        detected_at=T0,
                    )
                )
            elif подготовка == "unproven":
                # Строку переписки убираем — номер как будто вписан ботом.
                await s.execute(
                    sa.delete(ClientPhoneCandidate).where(
                        ClientPhoneCandidate.client_id == cards[1].id
                    )
                )
            elif подготовка == "stub":
                cards[1].external_id = "chat:zzz"
            elif подготовка == "blocked":
                cards[1].blocked_at = T0
            elif подготовка == "phone_conflict":
                cards[1].link_phone_conflict_at = T0
            elif подготовка == "cross_account":
                cards[0].cross_account_since = T0
            elif подготовка == "has_children":
                s.add(
                    Client(
                        channel="avito",
                        external_id="5009",
                        name="Кто-то",
                        merged_into_id=cards[0].id,
                        merged_at=T0,
                    )
                )
            elif подготовка == "no_messages":
                from app.models import Message

                await s.execute(sa.delete(Message))
            await s.commit()
    assert await задача(db_sessionmaker, redis) == f"skipped:{причина}"
    пара = [
        c for c in await read(sa.select(Client)) if c.external_id in ("5001", "5002", "chat:zzz")
    ]
    assert [c.merged_into_id for c in пара] == [None, None]


async def test_тень_пишет_строку_и_не_объединяет(
    db, redis, account, account2, read, db_sessionmaker
):
    await _настроить(db, CLIENT_MERGE_AUTO="shadow")
    await _пара(db, redis, account, account2)
    assert await задача(db_sessionmaker, redis) == "shadow"
    assert not [c for c in await read(sa.select(Client)) if c.merged_into_id is not None]
    (row,) = await read(sa.select(AuditLog).where(AuditLog.action == "client.merge_shadow"))
    assert row.user_id is None and row.details["rule"] == client_merge.RULE


async def test_выключено_молчит(db, redis, account, account2, read, db_sessionmaker):
    await _настроить(db, CLIENT_MERGE_AUTO="off")
    await _пара(db, redis, account, account2)
    assert await задача(db_sessionmaker, redis) == "skipped:off"


async def test_разъединение_запоминается_и_возвращает_поздние_диалоги(
    db, redis, account, account2, read, db_sessionmaker, users_by_role
):
    """⚠ ДВЕ ДИВЕРСИИ: убрать veto → пара склеится снова; убрать возврат по
    `origin_client_id` → диалог, заведённый после объединения, останется у победителя."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    assert await задача(db_sessionmaker, redis) == "merged"
    cards = await read(sa.select(Client).order_by(Client.external_id))
    winner, loser = cards[0], cards[1]
    assert loser.merged_into_id == winner.id
    # Новый чат на идентификатор ПРОИГРАВШЕЙ: диалог уходит к победителю и помнит исток.
    await apply_inbound_event(
        db,
        redis,
        account2,
        событие(
            "ещё вопрос",
            author=5002,
            chat="chat-c",
            account_user=222333444,
            at=T0 + timedelta(days=2),
        ),
    )
    (поздний,) = await read(
        sa.select(Conversation).where(Conversation.external_chat_id == "chat-c")
    )
    assert (поздний.client_id, поздний.origin_client_id) == (winner.id, loser.id)

    async with db.begin():
        lo = await db.get(Client, loser.id)
        итог = await clients_svc.unmerge_clients(db, loser=lo, actor=users_by_role["manager"])
    assert итог["moved_conversations"] == 2, "и снимок, и поздний диалог"
    (поздний,) = await read(
        sa.select(Conversation).where(Conversation.external_chat_id == "chat-c")
    )
    assert (поздний.client_id, поздний.origin_client_id) == (loser.id, None)
    (veto,) = await read(sa.select(ClientMergeVeto))
    assert {veto.a_id, veto.b_id} == {winner.id, loser.id}
    (row,) = await read(sa.select(AuditLog).where(AuditLog.action == "client.unmerged"))
    assert row.details["was_auto"] is True and row.details["returned_after_merge"] == 1

    # Та же пара снова удовлетворяет правилу — но человек уже сказал «нет».
    assert await задача(db_sessionmaker, redis) == "skipped:vetoed"
    async with db_sessionmaker() as s:
        подсказки = await clients_svc.merge_candidates(s, await s.get(Client, winner.id))
    assert [(p["id"], p["vetoed"]) for p in подсказки] == [(str(loser.id), True)]

    # Ручное «Объединить» переигрывает откат: запрет снимается.
    async with db.begin():
        w = await db.get(Client, winner.id)
        lo = await db.get(Client, loser.id)
        await clients_svc.merge_clients(db, winner=w, loser=lo, actor=users_by_role["manager"])
    assert await read(sa.select(ClientMergeVeto)) == []


async def test_автостоп_по_разъединениям_переводит_в_тень(
    db, redis, account, account2, read, db_sessionmaker
):
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    async with db_sessionmaker() as s:
        for _ in range(client_merge.AUTOSTOP_UNMERGES_PER_DAY):
            s.add(
                AuditLog(
                    user_id=None,
                    action="client.unmerged",
                    entity="client",
                    entity_id="x",
                    details={"was_auto": True},
                    created_at=datetime.now(UTC),
                )
            )
        await s.commit()
    assert await задача(db_sessionmaker, redis) == "shadow"
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.CLIENT_MERGE_AUTO) == "shadow"


async def test_заявка_объединённой_карточки_с_одним_номером_не_встаёт(
    db, redis, account, account2, read, db_sessionmaker
):
    """`lead_phone` 3′: все присоединённые назвали тот же номер — номер доказан.
    ⚠ ДИВЕРСИЯ: вернуть безусловное `None` при `merged_in` — краснеет."""
    from app.services.leads import lead_phone

    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    assert await задача(db_sessionmaker, redis) == "merged"
    cards = await read(sa.select(Client).order_by(Client.external_id))
    winner = cards[0]
    async with db_sessionmaker() as s:
        # Новый диалог победителя без единой строки-кандидата — путь 1 и 2 молчат.
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-new",
            account_id=account.id,
            client_id=winner.id,
            status="new",
            last_message_at=datetime.now(UTC),
        )
        s.add(conv)
        await s.commit()
        assert await lead_phone(s, conv, await s.get(Client, winner.id)) == A
        # Присоединённая карточка с ДРУГИМ номером — молчим, как раньше.
        await s.execute(sa.update(Client).where(Client.id == cards[1].id).values(phone=B))
        await s.commit()
        assert await lead_phone(s, conv, await s.get(Client, winner.id)) is None


async def test_ночной_проход_ставит_пары_только_ночью_и_помнит_проверенные(
    db, redis, account, account2, db_sessionmaker, monkeypatch
):
    from app.scheduler.jobs import merge_backlog
    from app.services import merge_queue

    monkeypatch.setattr(merge_backlog.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(merge_backlog.db_mod, "session_scope", db_sessionmaker)
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    # Живой путь уже поставил свою задачу — снимаем её, ночной проход ставит свою.
    await redis.delete(f"arq:job:{merge_queue.job_id(A)}")
    день = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    ночь = datetime(2026, 9, 12, 23, 30, tzinfo=UTC)
    assert await merge_backlog.merge_backlog(now=день) == 0
    assert await merge_backlog.merge_backlog(now=ночь) == 1
    assert await redis.exists(f"arq:job:{merge_queue.job_id(A)}")
    # Задача смотрела пару — следующей ночью проход её пропускает.
    await merge_queue.mark_seen(redis, A)
    await redis.delete(f"arq:job:{merge_queue.job_id(A)}")
    assert await merge_backlog.merge_backlog(now=ночь) == 0


def test_задача_объединения_зарегистрирована_у_воркера_и_у_планировщика() -> None:
    import ast
    import inspect
    import textwrap

    from app.scheduler import main as scheduler_main
    from app.workers.main import WorkerSettings

    (задача,) = [f for f in WorkerSettings.functions if getattr(f, "name", "") == "merge_twins"]
    assert задача.keep_result_s == 0 and задача.max_tries == 1
    дерево = ast.parse(textwrap.dedent(inspect.getsource(scheduler_main)))
    зовут = {
        f"{getattr(у.func.value, 'id', '')}.{у.func.attr}"
        for у in ast.walk(дерево)
        if isinstance(у, ast.Call) and isinstance(у.func, ast.Attribute)
    }
    assert "merge_backlog_jobs.register" in зовут


async def test_ручное_объединение_шлёт_кадры_после_commit(
    client, tokens, redis, account, account2, db, read
):
    await _настроить(db)
    await _пара(db, redis, account, account2, name2="Пётр")
    cards = await read(sa.select(Client).order_by(Client.external_id))
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await client.post(
        f"/api/v1/clients/{cards[0].id}/merge",
        json={"source_id": str(cards[1].id)},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text
    кадры = await drain_events(pubsub)
    assert [e["data"]["reason"] for e in кадры if e["type"] == "client:updated"] == [
        "merged",
        "merged",
    ]
    # Переехавший диалог и собственный диалог победителя (проверка 24.09).
    assert [
        e["data"]["patch"]["client"]["id"] for e in кадры if e["type"] == "conversation:updated"
    ] == [str(cards[0].id)] * 2


# --- правки по ревью 12.09 ------------------------------------------------------


async def test_номер_не_едет_аргументом_задачи_а_лежит_под_ключом(redis, monkeypatch) -> None:
    """ARQ печатает аргументы задачи в журнал воркера — номер клиента там быть не должен.
    ⚠ ДИВЕРСИЯ: передать `phone` вместо `key` в `enqueue_job` — краснеет."""
    from arq.connections import ArqRedis

    from app.services import merge_queue

    вызовы: list[tuple] = []

    async def подсмотреть(self, name, *args, **kwargs):  # noqa: ANN001
        вызовы.append((name, args, kwargs))
        return object()

    monkeypatch.setattr(ArqRedis, "enqueue_job", подсмотреть)
    assert await merge_queue.enqueue_merge(redis, A, defer_sec=0, backfill=True)
    ((name, args, kwargs),) = вызовы
    assert name == "merge_twins"
    assert args == (merge_queue.phone_key(A),) and A[2:] not in str(args)
    assert kwargs["backfill"] is True and A[2:] not in kwargs["_job_id"]
    assert await merge_queue.phone_for(redis, merge_queue.phone_key(A)) == A


async def test_память_смотрели_ставится_только_ночным_проходом(
    db, redis, account, account2, db_sessionmaker
):
    """Живой путь ставит задачу, когда второй карточки может ещё не быть: пометить пару
    «смотрели» на неделю значило бы отобрать её у ночного прохода.
    ⚠ ДИВЕРСИЯ: ставить mark_seen без условия `backfill` — краснеет."""
    from app.services import merge_queue

    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await apply_inbound_event(db, redis, account, событие(A[2:], author=5001, chat="chat-a"))
    assert await задача(db_sessionmaker, redis) == "skipped:single"
    assert not await redis.exists(merge_queue.seen_key(A))
    assert await задача(db_sessionmaker, redis, backfill=True) == "skipped:single"
    assert await redis.exists(merge_queue.seen_key(A))


async def test_запрет_появившийся_между_проверкой_и_замком_останавливает(
    db, redis, account, account2, db_sessionmaker, monkeypatch
):
    """Оператор нажал «Разъединить» после первой проверки, но до замка: строка
    запрета появляется — вторая проверка под замком её видит.
    ⚠ ДИВЕРСИЯ: убрать повторный `check_twins` после `lock_cards` — краснеет."""
    from app.services import client_merge as cm

    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    cards = await read_cards(db_sessionmaker)
    настоящий = cm.lock_cards

    async def замок_с_запретом(db_, *cards_):  # noqa: ANN001
        async with db_sessionmaker() as s:
            s.add(
                ClientMergeVeto(
                    a_id=min(cards[0].id, cards[1].id),
                    b_id=max(cards[0].id, cards[1].id),
                    created_at=datetime.now(UTC),
                )
            )
            await s.commit()
        await настоящий(db_, *cards_)

    monkeypatch.setattr(cm, "lock_cards", замок_с_запретом)
    assert await задача(db_sessionmaker, redis) == "skipped:vetoed"


async def read_cards(db_sessionmaker):
    async with db_sessionmaker() as s:
        return list((await s.execute(sa.select(Client).order_by(Client.external_id))).scalars())


async def test_автостоп_не_перебивает_повторное_включение_владельцем(
    db, redis, account, account2, read, db_sessionmaker
):
    """Откаты ДО последнего изменения настройки не считаются: владелец включил
    объединение обратно, зная о них."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    async with db_sessionmaker() as s:
        for _ in range(client_merge.AUTOSTOP_UNMERGES_PER_DAY):
            s.add(
                AuditLog(
                    user_id=None,
                    action="client.unmerged",
                    entity="client",
                    entity_id="x",
                    details={"was_auto": True},
                    created_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
        await s.commit()
    # Настройка менялась ПОЗЖЕ откатов (только что, в `_настроить`) — они не в счёт.
    assert await задача(db_sessionmaker, redis) == "merged"


async def test_автостоп_уведомляет_администратора(
    db, redis, account, account2, read, db_sessionmaker
):
    from app.models import Notification

    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    async with db_sessionmaker() as s:
        for _ in range(client_merge.AUTOSTOP_UNMERGES_PER_DAY):
            s.add(
                AuditLog(
                    user_id=None,
                    action="client.unmerged",
                    entity="client",
                    entity_id="x",
                    details={"was_auto": True},
                    created_at=datetime.now(UTC) + timedelta(seconds=5),
                )
            )
        await s.commit()
    assert await задача(db_sessionmaker, redis) == "shadow"
    (n,) = await read(
        sa.select(Notification).where(Notification.kind == "client_merge.autostopped")
    )
    assert n.severity == "warning"


async def test_отказ_сторожа_объединения_в_воркере_это_исход_а_не_падение(
    db, redis, account, account2, db_sessionmaker, monkeypatch
):
    from app.core.errors import ApiError
    from app.services import client_merge as cm

    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)

    async def упасть(*a, **kw):  # noqa: ANN001
        raise ApiError("already_merged", "уже", status=422)

    monkeypatch.setattr(cm, "merge_clients", упасть)
    assert await задача(db_sessionmaker, redis) == "skipped:already_merged"


async def test_починка_старых_предложений_заполняет_пустой_основной(
    db, redis, account, read, db_sessionmaker
):
    """Старое предложение на ПУСТОЙ карточке — первый номер, он идёт в основной.
    ⚠ ДИВЕРСИЯ: index=1 в classify внутри CLI — краснеет (номер лёг бы «дополнительным
    без основного»)."""
    from app.cli import run_phone_backlog

    await _настроить(db)
    await app_settings.set_many(db, {app_settings.PHONE_DETECT_AUTOFILL: False}, user_id=None)
    await db.commit()
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    (row,) = await read(sa.select(ClientPhoneCandidate))
    assert row.status == "pending"
    async with db_sessionmaker() as s:
        await run_phone_backlog(s, dry_run=False)
    (card,) = await read(sa.select(Client))
    assert card.phone == A
    (row,) = await read(sa.select(ClientPhoneCandidate))
    assert row.status == "accepted"
    захваты = await read(sa.select(AuditLog).where(AuditLog.action == "client.phone_captured"))
    assert len(захваты) == 1 and захваты[0].details["backfill"] is True


async def test_снятый_номер_возвращается_кнопкой(
    db, redis, account, read, users_by_role, db_sessionmaker
):
    """«Не его номер» обратимо: «добавить» на отклонённой строке возвращает её.
    ⚠ ДИВЕРСИЯ: убрать ветку `возвращаем_снятый` — 409 и краснеет."""
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие(A[2:]))
    await apply_inbound_event(
        db, redis, account, событие("ещё " + B[2:], at=T0 + timedelta(minutes=1))
    )
    (строка,) = await read(sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.phone == B))
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, строка.id)
        await clients_svc.resolve_phone_candidate(
            db, row, decision="reject", actor=users_by_role["manager"]
        )
    (card,) = await read(sa.select(Client))
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, card.id))
    assert [r["value"] for r in view["rejected_phones"]] == [B]
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, строка.id)
        итог = await clients_svc.resolve_phone_candidate(
            db, row, decision="add", actor=users_by_role["manager"]
        )
    assert итог["candidate"]["status"] == "accepted"
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, card.id))
    assert view["rejected_phones"] == []
    assert B in [p["value"] for p in view["phones"]]


async def test_ручной_номер_и_замена_ставят_проверку_двойников(
    client, tokens, redis, account, db, read, monkeypatch
):
    """Двойники проверяются не только с входящего: ручной ввод и «Заменить» тоже
    дают основной. ⚠ ДИВЕРСИЯ: убрать `enqueue_merge` из ручки телефона — краснеет."""
    from app.api.routes import clients as routes

    вызовы: list[str] = []

    async def подсмотреть(redis_, phone, **kw):  # noqa: ANN001
        вызовы.append(phone)
        return True

    monkeypatch.setattr(routes, "enqueue_merge", подсмотреть)
    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("здравствуйте"))
    (card,) = await read(sa.select(Client))
    r = await client.put(
        f"/api/v1/clients/{card.id}/phone",
        json={"phone": A},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text
    assert вызовы == [A]


# --- группа: три карточки с одним номером (владелец 13.09) --------------------


@pytest.fixture
async def account3(make_avito_account):
    return await make_avito_account(333444555, title="LP-Test-3")


async def _тройка(db, redis, account, account2, account3, *, третье_имя="Иван 1001030"):  # noqa: ANN001
    await apply_inbound_event(
        db, redis, account, событие(A[2:], author=5001, chat="chat-a", name="Иван 100101")
    )
    await apply_inbound_event(
        db,
        redis,
        account2,
        событие(
            A[2:],
            author=5002,
            chat="chat-b",
            name="Иван 100102",
            at=T0 + timedelta(minutes=1),
            account_user=222333444,
        ),
    )
    await apply_inbound_event(
        db,
        redis,
        account3,
        событие(
            A[2:],
            author=5003,
            chat="chat-c",
            name=третье_имя,
            at=T0 + timedelta(minutes=2),
            account_user=333444555,
        ),
    )


async def test_группа_с_номерами_заявок_в_именах_склеивается_в_старшую(
    db, redis, account, account2, account3, read, db_sessionmaker
):
    """«Иван 100101», «Иван 100102», «Иван 1001030» — один человек, три
    наших аккаунта, номера заявок в именах не считаются именем."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _тройка(db, redis, account, account2, account3)
    assert await задача(db_sessionmaker, redis) == "merged"
    cards = await read(sa.select(Client).order_by(Client.external_id))
    живые = [c for c in cards if c.merged_into_id is None]
    assert len(живые) == 1 and живые[0].external_id == "5001", "старшая по истории"
    assert {c.merged_into_id for c in cards if c.merged_into_id} == {живые[0].id}
    convs = await read(sa.select(Conversation))
    assert {c.client_id for c in convs} == {живые[0].id}
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, живые[0].id))
    assert len(view["merged_from"]) == 2 and all(m["auto"] for m in view["merged_from"])


async def test_третья_без_доказательства_останавливает_группу(
    db, redis, account, account2, account3, read, db_sessionmaker
):
    """Третья карточка с тем же номером, но номер вписан не из её переписки —
    группа не склеивается вовсе: правило одно на всех."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    async with db_sessionmaker() as s:
        s.add(Client(channel="avito", external_id="5003", name="Иван Петров", phone=A))
        await s.commit()
    assert await задача(db_sessionmaker, redis) in ("skipped:unproven", "skipped:no_messages")
    assert not [c for c in await read(sa.select(Client)) if c.merged_into_id is not None]


async def test_разъединение_одной_пары_группы_запирает_всю_группу(
    db, redis, account, account2, account3, read, db_sessionmaker
):
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _тройка(db, redis, account, account2, account3)
    cards = await read(sa.select(Client).order_by(Client.external_id))
    async with db_sessionmaker() as s:
        a, b = sorted((cards[1].id, cards[2].id))
        s.add(ClientMergeVeto(a_id=a, b_id=b, created_at=T0))
        await s.commit()
    assert await задача(db_sessionmaker, redis) == "skipped:vetoed"


async def test_cli_merge_auto_включает_режим_с_записью_в_журнал(db, read, db_sessionmaker):
    from app.cli import run_merge_auto

    async with db_sessionmaker() as s:
        await run_merge_auto(s, mode="on")
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.CLIENT_MERGE_AUTO) == "on"
    (row,) = await read(
        sa.select(AuditLog).where(AuditLog.action == "settings.phone_detect_changed")
    )
    assert row.details["after"] == {"merge_auto": "on"} and row.details["via"] == "cli"
    with pytest.raises(typer.Exit):
        async with db_sessionmaker() as s:
            await run_merge_auto(s, mode="always")


async def test_backfill_cards_догоняет_второй_номер_и_адрес_из_старой_переписки(
    db, redis, account, read, db_sessionmaker
):
    """Сообщение до правил 12.09: «лермонтова 5, кв 37 / мой номер … / номер жены …»
    (бой 30.08). Догон кладёт номер жены дополнительным и адрес — строкой на
    проверку; основной, уже стоящий в карточке, не трогает."""
    from app.cli import run_backfill_cards
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "лермонтова 5, кв 37\\nмой номер 89001112254\\nномер жены 89001112256",
            author=5001,
            chat="chat-a",
            at=T0 - timedelta(days=10),
        ),
    )
    # Как было на бою до 12.09: в карточке только основной, ни строки жены, ни адреса.
    async with db_sessionmaker() as s:
        await s.execute(
            sa.delete(ClientPhoneCandidate).where(ClientPhoneCandidate.phone == "+79001112256")
        )
        await s.execute(sa.delete(ClientAddressCandidate))
        await s.commit()
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=30, dry_run=True)
    assert await read(sa.select(ClientAddressCandidate)) == []
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=30, dry_run=False)
    (card,) = await read(sa.select(Client))
    assert card.phone == "+79001112254"
    номера = {
        (r.phone, r.status, r.hint)
        for r in await read(
            sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.client_id == card.id)
        )
    }
    assert ("+79001112256", "accepted", "жены") in номера
    (адрес,) = await read(sa.select(ClientAddressCandidate))
    assert (адрес.value, адрес.level, адрес.office) == ("лермонтова, 5", "A", "37")
    assert адрес.geo_status == "pending"


# --- находки ревью 13.09: что группа НЕ должна обходить -----------------------


async def test_третий_автор_на_аккаунте_поглощённой_не_склеивается(
    db, redis, account, account2, read, db_sessionmaker
):
    """A(acct1)+B(acct2) склеены; C — другой автор на том же acct2 с тем же
    номером и именем. Живыми втроём это был бы same_account; через поглощённую
    B обход недопустим."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _пара(db, redis, account, account2)
    assert await задача(db_sessionmaker, redis) == "merged"
    await apply_inbound_event(
        db,
        redis,
        account2,
        событие(
            A[2:], author=5003, chat="chat-c", account_user=222333444, at=T0 + timedelta(days=2)
        ),
    )
    assert await задача(db_sessionmaker, redis) == "skipped:same_account"
    живые = [c for c in await read(sa.select(Client)) if c.merged_into_id is None]
    assert {c.external_id for c in живые} == {"5001", "5003"}


async def test_вето_между_живой_и_поглощённой_держит_группу(
    db, redis, account, account2, account3, read, db_sessionmaker
):
    """Человек разъединил B и X; X руками увели в A — B→A всё равно нельзя."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _тройка(db, redis, account, account2, account3)
    cards = {c.external_id: c for c in await read(sa.select(Client))}
    a, b, x = cards["5001"], cards["5002"], cards["5003"]
    async with db_sessionmaker() as s:
        s.add(ClientMergeVeto(a_id=min(b.id, x.id), b_id=max(b.id, x.id), created_at=T0))
        # X руками увели в A: поглощённая с тем же номером.
        xx = await s.get(Client, x.id)
        xx.merged_into_id = a.id
        xx.merged_at = T0
        await s.execute(
            sa.update(Conversation).where(Conversation.client_id == x.id).values(client_id=a.id)
        )
        await s.commit()
    assert await задача(db_sessionmaker, redis) == "skipped:vetoed"


async def test_проигравшая_с_детьми_того_же_номера_именованный_отказ(
    db, redis, account, account2, account3, read, db_sessionmaker
):
    """Дети того же номера — «своя история» только у ПОБЕДИТЕЛЯ: merge_clients
    запрещает цепочку A→B→C, и без именованного отказа ночной проход
    возвращался бы к номеру вечно."""
    await _настроить(db, CLIENT_MERGE_AUTO="on")
    await _тройка(db, redis, account, account2, account3)
    cards = {c.external_id: c for c in await read(sa.select(Client))}
    async with db_sessionmaker() as s:
        s.add(
            Client(
                channel="avito",
                external_id="5009",
                name="Иван",
                phone=A,
                merged_into_id=cards["5003"].id,
                merged_at=T0,
            )
        )
        await s.commit()
    assert await задача(db_sessionmaker, redis) == "skipped:has_children"


async def test_backfill_cards_не_даёт_автозаписи_по_давнему_адресу(
    db, redis, account, read, db_sessionmaker
):
    from app.cli import run_backfill_cards
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("ул Ленина 5, кв 3", author=5001, chat="chat-a", at=T0 - timedelta(days=10)),
    )
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(ClientAddressCandidate))
        await s.commit()
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=30, dry_run=False)
    (строка,) = await read(sa.select(ClientAddressCandidate))
    assert строка.detected_at.replace(tzinfo=UTC) == T0 - timedelta(days=10)


async def test_address_reparse_дописывает_пункт_в_старую_строку(
    db, redis, account, read, db_sessionmaker
):
    """Строка хранит разбор своего дня: «Поселок Сосново дом 9» лёг без пункта.
    Перечитывание новым разбором дописывает пункт и возвращает строку в очередь;
    ключ «улица, дом» не меняется."""
    from app.cli import run_address_reparse
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(
        db, redis, account, событие("Поселок Сосново дом 9", author=5001, chat="chat-a")
    )
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert row.settlement == "Сосново"
        # Как было до правки разбора: пункта нет, карта уже отказала.
        row.settlement = None
        row.settlement_type = None
        row.geo_status = "elsewhere"
        row.geo_attempts = 2
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False)
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.value, row.settlement, row.settlement_type) == (
        "Поселок Сосново, 9",
        "Сосново",
        "посёлок",
    )
    assert row.geo_status == "pending" and row.geo_attempts == 0


async def test_address_reparse_меняет_ключ_нерешённой_строки(
    db, redis, account, read, db_sessionmaker
):
    """«Д. Сорокино улица Полевая дом 37» лёг с улицей «Д. Сорокино улица
    Полевая»; новый разбор выделяет деревню — нерешённой строке ключ
    меняется, если такого у карточки нет."""
    from app.cli import run_address_reparse
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(
        db, redis, account, событие("Д. Сорокино улица Полевая дом 37", author=5001, chat="chat-a")
    )
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        row.value, row.street = "Д. Сорокино улица Полевая, 37", "Д. Сорокино улица Полевая"
        row.settlement = None
        row.settlement_type = None
        row.geo_status = "elsewhere"
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False)
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.value, row.settlement, row.settlement_type, row.geo_status) == (
        "улица Полевая, 37",
        "Сорокино",
        "деревня",
        "pending",
    )


async def test_address_reparse_меняет_дом_только_у_отказанной_строки(
    db, redis, account, read, db_sessionmaker
):
    """«днт Ромашка, 5» (бой 13.09, Бурятия): старый разбор взял «5» домом, карта
    отказала. Новый разбор даёт «5 ул, 147» — дом меняется только потому, что
    строке уже отказано; у строки с exact дом не трогаем."""
    from app.cli import run_address_reparse
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Кедрово днт Ромашка 5 ул дом 147", author=5001, chat="chat-a"),
    )
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert (row.street, row.house) == ("5 ул", "147")
        # Как было до правки: «днт Ромашка, 5», карта отказала.
        row.value, row.street, row.house = "днт Ромашка, 5", "днт Ромашка", "5"
        row.settlement = None
        row.geo_status = "region_mismatch"
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False)
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.value, row.street, row.house, row.settlement) == (
        "5 ул, 147",
        "5 ул",
        "147",
        "Ромашка",
    )
    assert row.geo_status == "pending"
    # А строке, подтверждённой картой, дом не меняем.
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        row.value, row.street, row.house = "днт Ромашка, 5", "днт Ромашка", "5"
        row.geo_status = "exact"
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False)
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.value, row.house) == ("днт Ромашка, 5", "5")


async def test_один_адрес_одна_строка(db, redis, account, read, db_sessionmaker):
    """«ул. Некрасова 6» → «Некрасова 6 кв 41» (бой 13.09): вторая реплика дописывает
    первую строку, а не заводит вторую; «Переулок» и «переулок» — тоже одно.
    «ул Ленина 5» и «проспект Ленина 5» — разные."""
    from app.models import ClientAddressCandidate
    from app.services import clients as clients_svc

    await _настроить(db)
    await apply_inbound_event(
        db, redis, account, событие("ул. Некрасова, 6", author=5001, chat="chat-a")
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Некрасова 6 кв 41, 2 подъезд", author=5001, chat="chat-a", at=T0 + timedelta(minutes=1)
        ),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Переулок Школьный 19", author=5001, chat="chat-a", at=T0 + timedelta(minutes=2)),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "переулок Школьный 19 кв 19", author=5001, chat="chat-a", at=T0 + timedelta(minutes=3)
        ),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("проспект Ленина 5", author=5001, chat="chat-a", at=T0 + timedelta(minutes=4)),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("ул Ленина 5", author=5001, chat="chat-a", at=T0 + timedelta(minutes=5)),
    )
    строки = await read(
        sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
    )
    assert [(r.value, r.office, r.entrance) for r in строки] == [
        ("ул. Некрасова, 6", "41", "2"),
        ("Переулок Школьный, 19", "19", None),
        ("проспект Ленина, 5", None, None),
        ("ул Ленина, 5", None, None),
    ]
    # Показ: старые дубли (до правки) схлопываются, части сливаются, строка
    # того же места, что записанный адрес, не показывается.
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().first()
        дубль = ClientAddressCandidate(
            client_id=card.id,
            conversation_id=строки[0].conversation_id,
            message_id=строки[0].message_id,
            message_at=строки[0].message_at,
            value="Некрасова, 6",
            street="Некрасова",
            house="6",
            floor="5",
            raw="Некрасова 6",
            level="B",
            status="pending",
            detected_at=строки[0].detected_at + timedelta(minutes=1),
            geo_status="exact",
            geo_formatted="ул Некрасова, 6, Бийск",
        )
        s.add(дубль)
        card.address = "мой адрес"
        card.address_candidate_id = строки[2].id  # «проспект Ленина, 5» записан
        await s.commit()
        view = await clients_svc.identity_view(s, card)
    показ = {к["value"]: к["parts"] for к in view["address_candidates"]}
    assert "проспект Ленина, 5" not in показ
    # Из двух строк одного места показана подтверждённая картой, части — от обеих.
    assert показ["Некрасова, 6"] == {"office": "41", "entrance": "2", "floor": "5"}
    assert "ул. Некрасова, 6" not in показ and "ул Ленина, 5" in показ
    # Разовая уборка: лишняя строка удалена, её части переехали в оставшуюся.
    from app.cli import run_address_dedup

    async with db_sessionmaker() as s:
        await run_address_dedup(s, dry_run=False)
    строки = await read(
        sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
    )
    assert sorted(r.value for r in строки) == sorted(
        ["Некрасова, 6", "Переулок Школьный, 19", "проспект Ленина, 5", "ул Ленина, 5"]
    )
    некрасова = next(r for r in строки if r.value == "Некрасова, 6")
    assert (некрасова.office, некрасова.entrance, некрасова.floor) == ("41", "2", "5")


async def test_повтор_адреса_переносит_строку_и_тип_уточняет(
    db, redis, account, read, db_sessionmaker
):
    """Ревью 13.09: «Ленина 5» (C) → в новом диалоге «проспект Ленина 5, кв 7»:
    строка одна, переехала в новый диалог, получила тип, уровень и квартиру и
    вернулась карте на перепроверку."""
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("Ленина 5", chat="chat-a"))
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.level, row.value) == ("C", "Ленина, 5")
    async with db_sessionmaker() as s:
        r = await s.get(ClientAddressCandidate, row.id)
        r.geo_status = "ambiguous"
        await s.commit()
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("проспект Ленина 5, кв 7", chat="chat-b", at=T0 + timedelta(days=1)),
    )
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert (row.value, row.street, row.level, row.office) == (
        "проспект Ленина, 5",
        "проспект Ленина",
        "A",
        "7",
    )
    assert row.geo_status == "pending"
    conv_b = (await read(sa.select(Conversation).where(Conversation.external_chat_id == "chat-b")))[
        0
    ]
    assert row.conversation_id == conv_b.id
    # Части следующей реплики того же диалога находят строку по новому месту.
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("подъезд 3", chat="chat-b", at=T0 + timedelta(days=1, minutes=1)),
    )
    (row,) = await read(sa.select(ClientAddressCandidate))
    assert row.entrance == "3"


async def test_address_dedup_бережёт_источник_карточки(db, redis, account, read, db_sessionmaker):
    """Строка, на которую смотрит карточка, не удаляется, даже если «хуже»."""
    from app.cli import run_address_dedup
    from app.models import ClientAddressCandidate

    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("ул Мира 3", chat="chat-a"))
    (первая,) = await read(sa.select(ClientAddressCandidate))
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().first()
        дубль = ClientAddressCandidate(
            client_id=card.id,
            conversation_id=первая.conversation_id,
            message_id=первая.message_id,
            message_at=первая.message_at,
            value="Мира, 3",
            street="Мира",
            house="3",
            office="9",
            raw="Мира 3",
            level="A",
            status="pending",
            detected_at=первая.detected_at + timedelta(minutes=1),
            geo_status="exact",
            geo_formatted="ул Мира, 3, Орск",
        )
        s.add(дубль)
        card.address = "ул Мира, 3"
        card.address_candidate_id = первая.id
        await s.commit()
        await run_address_dedup(s, dry_run=False)
    строки = await read(sa.select(ClientAddressCandidate))
    assert [r.id for r in строки] == [первая.id]
    assert строки[0].office == "9"


async def test_карта_главнее_слов_два_подтверждённых_дома_не_дубль(
    db, redis, account, read, db_sessionmaker
):
    """«Ленина 5» → «улица Ленина, 5» и «проспект Ленина 5» → «проспект Ленина, 5»:
    обе подтверждены картой на разных домах — уборка их не сводит, показ тоже."""
    from app.cli import run_address_dedup
    from app.models import ClientAddressCandidate
    from app.services import clients as clients_svc

    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("Ленина 5", chat="chat-a"))
    (row,) = await read(sa.select(ClientAddressCandidate))
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().first()
        r = await s.get(ClientAddressCandidate, row.id)
        r.geo_status, r.geo_formatted, r.geo_lat, r.geo_lon = (
            "exact",
            "улица Ленина, 5, Орск",
            51.2,
            58.5,
        )
        s.add(
            ClientAddressCandidate(
                client_id=card.id,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                message_at=row.message_at,
                value="проспект Ленина, 5",
                street="проспект Ленина",
                house="5",
                raw="проспект Ленина 5",
                level="A",
                status="pending",
                detected_at=row.detected_at + timedelta(minutes=1),
                geo_status="exact",
                geo_formatted="проспект Ленина, 5, Орск",
                geo_lat=51.3,
                geo_lon=58.6,
            )
        )
        await s.commit()
        view = await clients_svc.identity_view(s, card)
        assert len(view["address_candidates"]) == 2
        await run_address_dedup(s, dry_run=False)
    assert len(await read(sa.select(ClientAddressCandidate))) == 2


async def test_дом_без_корпуса_при_записанном_с_корпусом_не_предлагается(
    db, redis, account, read, db_sessionmaker
):
    """«Чкалова 4» → карточка «ул Чкалова, 4 к 3» (бой 13.09): строка
    «Чкалова, 4» без корпуса не предлагается — записанное точнее."""
    from app.models import ClientAddressCandidate
    from app.services import clients as clients_svc

    await _настроить(db)
    await apply_inbound_event(db, redis, account, событие("Чкалова 4", chat="chat-a"))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Адрес Чкалова 4 кор 3", chat="chat-a", at=T0 + timedelta(minutes=1)),
    )
    строки = await read(
        sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
    )
    assert [r.value for r in строки] == ["Чкалова, 4", "Чкалова, 4 кор 3"]
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().first()
        card.address = "ул Чкалова, 4 к 3, Тюмень"
        card.address_candidate_id = строки[1].id
        r = await s.get(ClientAddressCandidate, строки[1].id)
        r.status = "accepted"
        await s.commit()
        view = await clients_svc.identity_view(s, card)
    assert view["address_candidates"] == []
