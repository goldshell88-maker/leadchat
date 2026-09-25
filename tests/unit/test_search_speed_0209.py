"""Поиск находит ровно то же, что и раньше, — и перестал вешать приложение.

⚠ ЖАЛОБА ВЛАДЕЛЬЦА 02.09: довести интерфейс до максимальной скорости и
надёжности. Замер боевой базы показал, где именно приложение встаёт: поиск.

    слово «телевизор»    566 мс
    имя «Иван»           913 мс
    короткое «ре»        570 мс
    ТЕЛЕФОН           15 314 мс
    «ремонт стиральной» 26 680 мс

Причина — форма условия. `OR` из четырёх проверок запрещает планировщику зайти
со стороны индекса: план давал Nested Loop по всем 42 686 диалогам с обходом 28
партиций `messages` на каждый. Больше миллиона обращений на один поиск, а поиск
идёт по мере набора — на номер из десяти цифр таких запросов уходило несколько.

После переписывания в UNION (та же выдача, другой вход для планировщика):

    телевизор  287 мс · Иван 98 мс · ре 101 мс · телефон 193 мс · фраза 295 мс

⚠ ЧТО ОХРАНЯЕТ ЭТОТ ФАЙЛ. Не скорость — её на SQLite не измерить, — а ТО, ЧТО
ВЫДАЧА НЕ ИЗМЕНИЛАСЬ. Ускорение ценой других результатов было бы не ускорением,
а потерей клиента: оператор скажет «вы к нам не обращались» человеку, который в
системе есть. Каждая из четырёх веток поиска проверяется отдельно, и отдельно
проверяется, что ветки не потеряли друг друга.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Client, ClientPhoneCandidate, Conversation, Message

T0 = datetime(2026, 9, 2, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def набор(db_sessionmaker, make_avito_account, users_by_role):
    """Четыре диалога — по одному на каждую ветку поиска — и один посторонний."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сделано = {}

        async def диалог(метка, *, имя, телефон=None, кандидат=None, текст=None, сдвиг=0):
            cl = Client(
                id=uuid.uuid4(),
                channel="avito",
                external_id=f"ext-{метка}",
                name=имя,
                phone=телефон,
            )
            s.add(cl)
            await s.flush()
            кандидат_ждёт = кандидат  # заполним после создания диалога: нужен его id
            conv = Conversation(
                id=uuid.uuid4(),
                channel="avito",
                external_chat_id=f"chat-{метка}",
                account_id=account.id,
                client_id=cl.id,
                status="in_progress",
                unread_count=0,
                tags=[],
                declined_by=[],
                bot_active=False,
                bot_vars={},
                last_message_at=T0 + timedelta(minutes=сдвиг),
            )
            s.add(conv)
            await s.flush()
            if кандидат_ждёт:
                s.add(
                    ClientPhoneCandidate(
                        client_id=cl.id,
                        conversation_id=conv.id,
                        phone=кандидат_ждёт,
                        raw=кандидат_ждёт,
                        detected_at=T0,
                    )
                )
            if текст:
                s.add(
                    Message(
                        conversation_id=conv.id,
                        external_message_id=f"am-{метка}",
                        direction="in",
                        sender_type="client",
                        body=текст,
                        attachments=[],
                        delivery_status="delivered",
                        created_at=T0 + timedelta(minutes=сдвиг),
                    )
                )
            сделано[метка] = conv.id
            return conv.id

        await диалог("имя", имя="Иннокентий Уникальный", сдвиг=40)
        await диалог("телефон", имя="Кто-то", телефон="+79151234567", сдвиг=30)
        await диалог("кандидат", имя="Ещё кто-то", кандидат="+79157654321", сдвиг=20)
        await диалог("текст", имя="Третий", текст="сломался холодильник Атлант", сдвиг=10)
        await диалог("посторонний", имя="Посторонний", текст="про погоду", сдвиг=0)
        await s.commit()
    return SimpleNamespace(**сделано)


async def найдено(client, tokens, q: str) -> set[str]:
    r = await client.get(f"/api/v1/conversations?tab=any&q={q}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


@pytest.mark.parametrize(
    ("запрос", "метка", "ветка"),
    [
        ("Иннокентий", "имя", "имя клиента"),
        ("9151234567", "телефон", "телефон клиента"),
        ("9157654321", "кандидат", "кандидат в телефоны"),
        ("холодильник", "текст", "текст переписки"),
    ],
)
async def test_каждая_ветка_поиска_жива(client, tokens, набор, запрос, метка, ветка):
    """⚠ ПО ОДНОЙ ПРОВЕРКЕ НА ВЕТКУ — ИНАЧЕ ПОТЕРЮ ОДНОЙ НЕ ЗАМЕТИТЬ.

    Веток четыре, и раньше они были слагаемыми одного `OR`. Разбери его на
    объединение неаккуратно — и пропадёт ровно одна, а остальные три будут
    зеленеть. Дороже всех потеря телефона: по нему ищут клиента, который звонит.
    """
    assert await найдено(client, tokens, запрос) == {str(getattr(набор, метка))}, (
        f"ветка «{ветка}» потерялась"
    )


async def test_ветки_не_съедают_друг_друга(client, tokens, набор):
    """Слово, которое подходит сразу двум веткам, находит ОБА диалога."""
    # «Атлант» есть только в тексте; проверяем, что объединение не схлопнулось
    # до первой сработавшей ветки.
    assert await найдено(client, tokens, "Атлант") == {str(набор.текст)}
    # Пустой результат остаётся пустым — объединение не тянет лишнего.
    assert await найдено(client, tokens, "Зазеркалье") == set()


async def test_посторонний_не_находится(client, tokens, набор):
    """Ускорение ценой лишних строк — тоже не ускорение."""
    for запрос in ("Иннокентий", "9151234567", "холодильник"):
        assert str(набор.посторонний) not in await найдено(client, tokens, запрос)


def test_условие_собрано_объединением_а_не_или() -> None:
    """⚠ ПРОВОДКА: форма условия и есть предмет правки.

    Выдачу можно сохранить и вернувшись к `OR` — тесты выше этого не заметят, а
    приложение снова будет вставать на полторы минуты. Поэтому форма проверяется
    прямо: в условии обязано быть объединение и обязана отсутствовать
    коррелированная проверка «есть ли в ЭТОМ диалоге такое сообщение».
    """
    from sqlalchemy.dialects import postgresql

    from app.services import conversations as convs

    # `_dialect` смотрит на живое соединение; здесь важна только ветка Postgres.
    sql = str(
        convs._search_condition(_ПсевдоPG(), "9151234567").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "UNION" in sql, "поиск вернулся к форме OR — приложение снова будет вставать"
    assert "websearch_to_tsquery" in sql, "ветка по тексту потерялась"
    # Признак прежней беды: подзапрос, привязанный к строке диалога.
    assert "messages.conversation_id = conversations.id" not in sql, (
        "вернулась коррелированная проверка по сообщениям — это и есть Nested Loop "
        "по всем диалогам с обходом 28 партиций"
    )


class _ПсевдоPG:
    """Соединение, о котором известно одно: диалект Postgres."""

    class _Bind:
        class dialect:  # noqa: N801 — подражаем форме SQLAlchemy
            name = "postgresql"

    def get_bind(self):  # noqa: ANN201
        return self._Bind()


async def test_список_без_поиска_не_трогает_таблицу_клиентов(client, tokens, набор) -> None:
    """⚠ САМАЯ ЧАСТАЯ РУЧКА СИСТЕМЫ НЕ ДОЛЖНА ПЛАТИТЬ ЗА ЧУЖОЙ ФИЛЬТР.

    Соединение с `clients` стояло в выборке списка всегда, хотя нужно было
    одному условию — поиску по имени. Замер боя 02.09: `count(*)` с
    соединением 193,9 мс против 13,3 мс без него, хэш на 3164 kB по 56 574
    клиентам. Это на каждой странице каждого списка у каждого из тринадцати.

    Выдача при этом не меняется: `conversations.client_id` объявлен NOT NULL и
    защищён внешним ключом, диалогов без клиента не бывает — внутреннее
    соединение не отсекало ни одной строки. Здесь это и проверяется: список без
    запроса отдаёт ВСЕ диалоги набора.
    """
    r = await client.get("/api/v1/conversations?tab=any&limit=200", headers=auth(tokens))
    assert r.status_code == 200, r.text
    отдано = {i["id"] for i in r.json()["items"]}
    все = {str(getattr(набор, м)) for м in ("имя", "телефон", "кандидат", "текст", "посторонний")}
    assert все <= отдано, "снятие соединения потеряло строки — этого быть не должно"
    # И карточка клиента в строке по-прежнему заполнена: её грузит отдельный
    # проход `_load_related`, а не соединение в выборке.
    по_id = {i["id"]: i for i in r.json()["items"]}
    assert по_id[str(набор.имя)]["client"]["name"] == "Иннокентий Уникальный"


def test_выборка_списка_собрана_без_соединения_с_клиентами() -> None:
    """⚠ ПРОВОДКА: соединение легко вернуть обратно «за компанию».

    Проверка выше этого не заметит — выдача-то не изменится. Заметит только
    боевой замер, а он бывает раз в месяц.
    """
    import inspect

    from app.services import conversations as convs

    исходник = inspect.getsource(convs.list_conversations)
    хвост = исходник[исходник.index("base = select(Conversation)") :]
    assert "join(Client" not in хвост, (
        "соединение с clients вернулось в выборку списка — это 194 мс вместо 13 "
        "на самой частой ручке системы"
    )
