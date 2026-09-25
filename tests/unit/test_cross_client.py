"""Один человек, пишущий на разные аккаунты Авито (требование владельца 6 от 11 августа).

ГЛАВНОЕ, ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Склейка клиентов между аккаунтами в системе
УЖЕ работает и работала всегда: `clients` ключуется парой `channel +
external_id` без аккаунта, а `_upsert_client` ищет клиента по ней же. То есть
первый тест этого файла — не про новую возможность, а про доказательство
поведения, которое до сегодня никто не проверял.

Опасность ровно обратная привычной: допущение «`author_id` Авито одинаков во
всех аккаунтах» ничем не подтверждено (в каталоге docs/26 такого утверждения
нет, спецификация про `Chat.users[].id` пишет только «Обратите внимание на
хэширование»). Если оно ложно, в одну карточку съезжаются посторонние люди
вместе с телефонами и перепиской. Поэтому вторая половина файла проверяет не
склейку, а ЧЕСТНОСТЬ РАССКАЗА О НЕЙ: `assumed` не должен становиться
`confirmed` без доказательства, а доказательством считается только телефон,
пришедший с ДРУГОГО канала.

ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждая порча прогонялась
отдельно, и каждая красила ровно свой тест): проверка происхождения телефона
`phone_account_id == account_id` в `_apply_phone_evidence`; ветка «номер не
совпал»; имя события `client.cross_account_linked`; отсечка повторного события
у уже отмеченной карточки; `summary.channels` в ответе истории; отдача
`link_confidence` из `conversation_out`; порядок каналов (по обращениям →
по алфавиту); сам CHECK на словарь значений; его имя (задвоенная приставка
`ck_clients_`).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.models import Client, Conversation
from app.models.client import LINK_ASSUMED, LINK_CONFIRMED
from app.services import inbound as inbound_module
from app.services.inbound import apply_inbound_event

try:  # настоящий разбор, когда OAuth-зона на месте
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

AVITO_A = 111222333  # аккаунт «Тимофей»
AVITO_B = 444555666  # аккаунт «Дамир»
AUTHOR = 999001  # тот самый author_id, на котором держится вся склейка
PHONE_TEXT = "Экран разбит. Мой номер 8 926 123-45-67"
OTHER_PHONE_TEXT = "Здравствуйте, звоните 8 916 000-11-22"
PHONE = "+79261234567"
T0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)


def make_event(account_user_id: int, **kw: Any) -> InboundEvent:
    defaults = {
        "external_chat_id": "chat-1",
        "external_message_id": "am-1",
        "author_id": AUTHOR,
        "account_user_id": account_user_id,
        "text": "Здравствуйте! Почём ремонт?",
        "created_at": T0,
        "client_name": "Иван Петров",
        "item_title": "Ремонт холодильника",
    }
    defaults.update(kw)
    return InboundEvent(**defaults)


class _LogSpy:
    """Подмена structlog-логгера: запоминает событие и его поля."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    info = _record
    warning = _record
    debug = _record
    exception = _record

    def events(self) -> list[str]:
        return [e for e, _ in self.calls]

    def fields_of(self, event: str) -> dict[str, Any]:
        return next(kw for e, kw in self.calls if e == event)


@pytest.fixture
async def account_a(make_avito_account):
    return await make_avito_account(AVITO_A, webhook_secret="whsec-a", title="Тимофей")


@pytest.fixture
async def account_b(make_avito_account):
    return await make_avito_account(AVITO_B, webhook_secret="whsec-b", title="Дамир")


@pytest.fixture
def read(db_sessionmaker):
    async def _read(stmt):
        async with db_sessionmaker() as session:
            return list((await session.execute(stmt)).scalars())

    return _read


@pytest.fixture
def write_to(db, redis):
    """Клиент пишет на аккаунт: одно входящее сообщение, как из вебхука."""

    async def _write(account, *, chat: str, msg: str, text: str, minutes: int = 0) -> bool:
        return await apply_inbound_event(
            db,
            redis,
            account,
            make_event(
                account.avito_user_id,
                external_chat_id=chat,
                external_message_id=msg,
                text=text,
                created_at=T0 + timedelta(minutes=minutes),
            ),
        )

    return _write


async def one_client(read) -> Client:
    clients = await read(select(Client))
    assert len(clients) == 1, f"ожидалась одна карточка, получено {len(clients)}"
    return clients[0]


# --- что происходит сегодня (доказательство поведения) -----------------------


async def test_same_author_on_two_accounts_reuses_one_card(write_to, account_a, account_b, read):
    """Один `author_id` с двух аккаунтов — ОДНА карточка и два диалога.

    Это и есть ответ на вопрос владельца «связываются ли клиенты»: связываются,
    и связывались всегда. Строка, которая это делает, — отбор в
    `_upsert_client` по `channel + external_id`, без аккаунта.
    """
    assert await write_to(account_a, chat="chat-a", msg="am-a", text="Почём ремонт?") is True
    assert await write_to(account_b, chat="chat-b", msg="am-b", text="И сюда напишу") is True

    client = await one_client(read)
    convs = await read(select(Conversation))
    assert {c.account_id for c in convs} == {account_a.id, account_b.id}
    assert {c.client_id for c in convs} == {client.id}
    # ...и карточка теперь ЗНАЕТ, что она межканальная, — до этой работы факт
    # существовал только в виде двух строк в `conversations`.
    assert client.cross_account_since is not None
    assert client.link_confidence == LINK_ASSUMED
    # Одного лишь идентификатора мало для «точно тот же»: пока телефон не
    # пришёл с обоих каналов, это предположение.
    assert client.link_phone_conflict_at is None


async def test_two_dialogs_on_one_account_are_not_a_cross_account_link(write_to, account_a, read):
    """Клиент вернулся по другому объявлению — это не межканальная склейка.

    Проверка ровно одного: признак не должен зажигаться от любого второго
    диалога. Иначе предупреждение появится у каждого повторного клиента,
    диспетчер привыкнет его пролистывать — и оно не сработает там, где нужно.
    """
    await write_to(account_a, chat="chat-1", msg="am-1", text="Холодильник")
    await write_to(account_a, chat="chat-2", msg="am-2", text="А ещё стиральная")

    client = await one_client(read)
    assert client.cross_account_since is None
    assert client.link_confidence is None


# --- наблюдение: первый случай не должен раствориться ------------------------


async def test_first_link_is_logged_once_with_both_accounts(
    monkeypatch, write_to, account_a, account_b, read
):
    """Первое срабатывание склейки — отдельная строка журнала с ОБОИМИ аккаунтами.

    Смысл события: на 11 августа в боевой базе 52 клиента и ни одного
    межканального, то есть механизм включён и ни разу не срабатывал. Первый
    настоящий случай — единственная возможность проверить допущение о
    сквозном `author_id`; без строки в журнале он растворится в потоке.
    """
    spy = _LogSpy()
    monkeypatch.setattr(inbound_module, "log", spy)

    await write_to(account_a, chat="chat-a", msg="am-a", text="Первое обращение")
    assert "client.cross_account_linked" not in spy.events()  # один канал — событий нет

    await write_to(account_b, chat="chat-b", msg="am-b", text="Второе обращение")
    assert spy.events().count("client.cross_account_linked") == 1
    fields = spy.fields_of("client.cross_account_linked")
    client = await one_client(read)
    assert fields["client_id"] == str(client.id)
    # Оба аккаунта в одной строке: без них случай нельзя ни воспроизвести, ни
    # проверить руками в кабинете Авито.
    assert {fields["account_id"], fields["other_account_id"]} == {
        str(account_a.id),
        str(account_b.id),
    }
    assert fields["avito_author_id"] == str(AUTHOR)
    assert fields["link_confidence"] == LINK_ASSUMED

    # Третий диалог на уже известном канале второго события не даёт: журнал
    # про ПЕРВОЕ срабатывание, а не про каждое сообщение постоянного клиента.
    await write_to(account_b, chat="chat-b2", msg="am-b2", text="И ещё раз")
    assert spy.events().count("client.cross_account_linked") == 1


# --- защита от ложной склейки ------------------------------------------------


async def test_same_phone_from_the_other_channel_confirms_the_link(
    write_to, account_a, account_b, read
):
    """Тот же номер с другого канала — это уже не догадка, а подтверждение.

    Совпасть у двух посторонних людей должны и идентификатор Авито, и телефон
    одновременно; это неправдоподобно, поэтому склейка объявляется
    подтверждённой.

    ПРОВЕРЯЕТСЯ ПРИ УМОЛЧАНИЯХ, А НЕ ПРИ ВКЛЮЧЁННОЙ ЗАПИСИ В КАРТОЧКУ. С
    правки 10 от 12 августа распознанный номер по умолчанию в `clients.phone`
    не пишется — он предлагается оператору. Если бы проверка склейки умела
    сравнивать только с карточкой, она умерла бы молча у всех и навсегда:
    сравнивать было бы не с чем. Поэтому тест намеренно НЕ включает
    `phone_detect.autofill` и требует подтверждения от системы, у которой
    карточка пуста.
    """
    await write_to(account_a, chat="chat-a", msg="am-a", text=PHONE_TEXT)
    await write_to(account_b, chat="chat-b", msg="am-b", text=f"Я же писал: {PHONE_TEXT}")

    client = await one_client(read)
    assert client.phone is None, "по умолчанию номер в карточку не пишется"
    assert client.link_confidence == LINK_CONFIRMED
    assert client.link_phone_conflict_at is None


async def test_different_phones_leave_the_link_presumed_and_mark_the_conflict(
    write_to, account_a, account_b, read
):
    """Разные телефоны у одного `author_id` — склейка ОСТАЁТСЯ предположительной.

    Это ровно тот случай, ради которого всё затевалось: если идентификатор
    Авито не сквозной, в одну карточку попадают два разных человека. Телефон —
    единственный сигнал, который сегодня может на это указать, и он обязан
    указывать, а не молчать.
    """
    await write_to(account_a, chat="chat-a", msg="am-a", text=PHONE_TEXT)
    await write_to(account_b, chat="chat-b", msg="am-b", text=OTHER_PHONE_TEXT)

    client = await one_client(read)
    # Карточка пуста: по умолчанию распознанное только предлагается (правка 10).
    # Спор при этом обязан быть замечен — иначе два разных человека под одной
    # карточкой останутся без единого признака беды.
    assert client.phone is None
    assert client.link_confidence == LINK_ASSUMED
    assert client.link_phone_conflict_at is not None


async def test_the_same_phone_from_the_same_channel_confirms_nothing(
    write_to, account_a, account_b, read
):
    """Номер, повторённый на ТОМ ЖЕ канале, — не доказательство, а тавтология.

    Без этой проверки «подтверждено телефоном» получал бы каждый клиент,
    дважды написавший свой номер в один и тот же чат, — то есть подтверждение
    выдавалось бы само себе.
    """
    await write_to(account_a, chat="chat-a", msg="am-a", text=PHONE_TEXT)
    await write_to(account_b, chat="chat-b", msg="am-b", text="Здравствуйте")
    await write_to(account_a, chat="chat-a", msg="am-a2", text=f"Повторю: {PHONE_TEXT}", minutes=5)

    client = await one_client(read)
    assert client.link_confidence == LINK_ASSUMED
    assert client.link_phone_conflict_at is None


async def test_phone_of_unknown_origin_neither_confirms_nor_disputes(
    db, redis, db_sessionmaker, account_a, account_b, read
):
    """У номера без известного канала доказательной силы нет — в обе стороны.

    Так выглядят строки, заведённые до миграции 0025, и телефоны, пойманные
    вторым писателем (`app/bots/engine.py::capture_phone`): значение есть,
    происхождение неизвестно. Достроить его задним числом нечем, поэтому
    молчим, а не додумываем.
    """
    async with db_sessionmaker() as session:
        session.add(
            Client(
                id=uuid.uuid4(),
                channel="avito",
                external_id=str(AUTHOR),
                name="Иван Петров",
                phone=PHONE,  # происхождение неизвестно: phone_account_id пуст
            )
        )
        await session.commit()

    for account, chat, msg in ((account_a, "chat-a", "am-a"), (account_b, "chat-b", "am-b")):
        await apply_inbound_event(
            db,
            redis,
            account,
            make_event(
                account.avito_user_id,
                external_chat_id=chat,
                external_message_id=msg,
                text=PHONE_TEXT,  # тот же номер — и всё равно не подтверждение
            ),
        )

    client = await one_client(read)
    assert client.phone_account_id is None
    assert client.link_confidence == LINK_ASSUMED
    assert client.link_phone_conflict_at is None


async def test_database_refuses_a_third_confidence_value(db_sessionmaker):
    """Третьего значения уверенности не существует — и база это стережёт.

    Колонка читается интерфейсом ровно двумя ветками: «предположительно» и
    «точно». Значение мимо словаря — например `'assumed '` с хвостовым
    пробелом от копипасты в psql — не покажется НИ ОДНОЙ из них: карточка
    молча потеряет предупреждение и станет выглядеть как обычный клиент. Это
    ровно тот класс беды, ради которого всё и затевалось, только наизнанку,
    поэтому сторож стоит в базе, а не в приложении: писателей у колонки
    больше, чем один (миграция, код, рука администратора).

    ИМЯ ОГРАНИЧЕНИЯ ПРОВЕРЯЕТСЯ НАРАВНЕ С ЕГО ДЕЙСТВИЕМ. В коде оно записано
    как `link_confidence`, а приставку `ck_clients_` добавляет соглашение об
    именах (`app/models/base.py`). Стоило написать имя вместе с приставкой — и
    в базу уезжало `ck_clients_ck_clients_link_confidence`: работает, но
    найти его в базе по имени из кода нельзя. Так и было в первой версии этой
    правки, поймано прогоном миграции на настоящем Postgres.
    """
    async with db_sessionmaker() as session:
        session.add(Client(channel="avito", external_id="777001", link_confidence="assumed "))
        with pytest.raises(IntegrityError):
            await session.commit()

    # Имя проверяется по СОБРАННОМУ DDL, а не по тексту ошибки: в тексте
    # `ck_clients_ck_clients_link_confidence` содержит `ck_clients_link_confidence`
    # как подстроку, и проверка вхождением пропустила бы ровно ту беду, ради
    # которой написана (сначала так и вышло — задвоение осталось незамеченным).
    ddl = str(CreateTable(Client.__table__).compile(dialect=sqlite_dialect()))
    assert "CONSTRAINT ck_clients_link_confidence CHECK" in ddl
    # Словарь в SQL и словарь в Python — два РАЗНЫХ текста, разъехаться им
    # ничто не мешает. Переименуй `LINK_ASSUMED` — и CHECK, не изменившись ни
    # на символ, начнёт отвергать всё, что пишет код: приём сообщений ляжет на
    # первом же клиенте, написавшем на второй аккаунт.
    assert f"'{LINK_ASSUMED}'" in ddl
    assert f"'{LINK_CONFIRMED}'" in ddl


# --- что видно снаружи (карточка клиента) ------------------------------------


async def test_history_and_card_show_both_channels_and_the_confidence(
    write_to, account_a, account_b, read, client, tokens
):
    """История клиента показывает оба канала, а карточка — меру доверия.

    До этой правки сервер считал историю и молчал о ней (дефект аудита
    SCEN-14), а канал прошлого обращения не показывался нигде: диспетчер видел
    список диалогов, не понимая, что часть из них пришла на другой аккаунт.
    """
    await write_to(account_a, chat="chat-a", msg="am-a", text="Первое обращение")
    # Через сорок минут тот же человек пишет на второй аккаунт: порядок каналов
    # в сводке — это порядок его обращений, а не алфавит.
    await write_to(account_b, chat="chat-b", msg="am-b", text="Второе обращение", minutes=40)
    convs = await read(select(Conversation))
    current = next(c for c in convs if c.account_id == account_b.id)
    past = next(c for c in convs if c.account_id == account_a.id)

    headers = {"Authorization": f"Bearer {tokens['manager']}"}
    detail = (await client.get(f"/api/v1/conversations/{current.id}", headers=headers)).json()
    # Предположение уезжает наружу ИМЕННО как предположение.
    assert detail["client"]["link_confidence"] == LINK_ASSUMED
    assert detail["client"]["link_phone_conflict"] is False
    assert detail["client_conversations_count"] == 2

    history = (
        await client.get(f"/api/v1/conversations/{current.id}/client-history", headers=headers)
    ).json()
    assert [i["id"] for i in history["items"]] == [str(past.id)]
    # Канал у каждого прошлого обращения — иначе история из двух каналов
    # читается как история одного.
    assert history["items"][0]["account"] == {"id": str(account_a.id), "title": "Тимофей"}
    summary = history["summary"]
    assert [c["title"] for c in summary["channels"]] == ["Тимофей", "Дамир"]  # в порядке прихода
    assert summary["link_confidence"] == LINK_ASSUMED
    assert summary["link_phone_conflict"] is False
    assert summary["cross_account_since"] is not None


# --- объединённая карточка на приёме (аудит 19.08, находка L-014) -------------


async def test_message_lands_on_the_final_card_after_merge(write_to, account_a, read, db):
    """Клиент с ОБЪЕДИНЁННОЙ карточкой пишет — сообщение садится на победителя.

    ЧТО БЫЛО. Приём находил карточку по внешнему ключу Авито и на признак
    объединения не смотрел ни разу: сообщение человека, чью карточку уже слили
    с другой, садилось на проигравшую. Переписка одного человека расползалась
    надвое, а диспетчер видел половину. На бою 16 объединений и две
    объединённые карточки — случай не теоретический.

    ⚠ ЭТОТ ТЕСТ ЗАВЕДЁН ПОТОМУ, ЧТО ПРАВКА ОДНАЖДЫ ПОТЕРЯЛАСЬ. Её написали,
    описали в сообщении коммита — и она уехала в отложенную стопку, которую
    удалили. Коммит про неё был, кода не было, и заметили это только сверкой
    боевого контейнера. Тест держит само свойство, а не память о правке.
    """
    await write_to(account_a, chat="chat-merge-1", msg="am-merge-1", text="Первое сообщение")
    карточки = await read(select(Client))
    assert len(карточки) == 1
    проигравшая = карточки[0]

    победитель = Client(
        id=uuid.uuid4(), channel="avito", external_id="999999001", name="Победитель"
    )
    db.add(победитель)
    await db.flush()
    # ⚠ Правим карточку В ТОЙ ЖЕ сессии, что и пишем: объект из `read` приехал
    # из другой сессии, и его изменения этот commit не сохранил бы (на это я
    # сам и попался, когда писал тест).
    своя = await db.get(Client, проигравшая.id)
    своя.merged_into_id = победитель.id
    своя.merged_at = datetime.now(UTC)
    await db.commit()

    await write_to(account_a, chat="chat-merge-2", msg="am-merge-2", text="Второе сообщение")

    диалоги = await read(
        select(Conversation).where(Conversation.external_chat_id == "chat-merge-2")
    )
    assert len(диалоги) == 1
    assert диалоги[0].client_id == победитель.id, (
        "сообщение село на объединённую карточку — переписка расползлась надвое"
    )
