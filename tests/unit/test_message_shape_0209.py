"""Сторож формы сообщения Авито: имена полей в лог, переписка — нет.

⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «сделать так, чтобы были видны сообщения, на которые
люди ответили ответным сообщением, и чтобы я мог отвечать так же». Это
цитирование, и первый вопрос — умеет ли его Авито вообще.

По вебхукам ответ уже есть и он твёрдый: в 90 777 боевых записях `webhook_raw_log`
слова «quote» нет ни разу. Но вебхук — уведомление, а не полная карточка
сообщения; переписку мы читаем другой ручкой, и её сырые ответы не сохраняются
нигде. Сторож закрывает эту дыру наблюдением, а не догадкой — тем же приёмом,
которым в этом проекте уже выяснили формат профиля клиента.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.avito import adapter as мод
from app.integrations.avito.adapter import AvitoAdapter, ChatInfo


class Шпион:
    def __init__(self) -> None:
        self.записи: list[dict[str, Any]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})

    def debug(self, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})

    def warning(self, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})


ЧАТ = ChatInfo(
    external_chat_id="u2i-1",
    client_external_id="923456789",
    client_name="Иван",
    item_title=None,
    item_url=None,
    item_price=None,
    has_unread=False,
    unread_count=0,
    last_message_at=None,
)


def сообщение(**лишнее: Any) -> dict[str, Any]:
    return {
        "id": "am-1",
        "author_id": 923456789,
        "created": 1_787_000_000,
        "type": "text",
        "content": {"text": "Здравствуйте"},
        **лишнее,
    }


@pytest.fixture(autouse=True)
def чистая_память(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(мод, "_ФОРМЫ_СООБЩЕНИЙ", set())
    yield


async def test_сторож_срабатывает_на_ответе_ручки(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ НАБЛЮДЕНИЕ, КОТОРОЕ МОЛЧИТ, НИЧЕМ НЕ ОТЛИЧАЕТСЯ ОТ ОТСУТСТВУЮЩЕГО.

    Сначала сторож стоял в `normalize_history_message` — у ОДНОГО из читателей
    ручки. За полчаса боя он не записал ни строки: разбор истории зовётся только
    при загрузке переписки, а сверка и досылка ходят к той же ручке своими
    путями. Теперь он стоит там, где ответ ручки превращается в список
    сообщений.

    ⚠ И ПРОВЕРЯЕТСЯ ЭТО ВЫЗОВОМ, А НЕ ГРЕПОМ ПО ИСХОДНИКУ. Здесь стоял тест,
    искавший имя функции в тексте `get_chat_messages`. Он пропустил диверсию:
    вхождений два (список и словарь), и снятие одного оставляло строку на месте.
    Проверка по тексту исходника снова доказала свою бесполезность.
    """
    from app.integrations.avito.client import AvitoClient

    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    class Ответ:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"messages": [сообщение(quote={"id": "am-0"})]}

    client = AvitoClient()

    async def _fake_request(*_a: Any, **_kw: Any) -> Ответ:
        return Ответ()

    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_raise_for_status", lambda *_a, **_kw: None)

    await client.get_chat_messages("t", 111, "u2i-1")

    формы = [з for з in шпион.записи if з["event"] == "avito.message_shape"]
    assert формы, "ответ ручки прошёл мимо сторожа — он снова будет молчать"
    assert "quote" in формы[0]["fields"]


def test_имена_полей_попадают_в_лог(monkeypatch: pytest.MonkeyPatch) -> None:
    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    AvitoAdapter.normalize_history_message(сообщение(), chat=ЧАТ, account_user_id=111)

    формы = [з for з in шпион.записи if з["event"] == "avito.message_shape"]
    assert формы, "форма сообщения обязана попасть в лог: иначе вопрос про цитату не закрыть"
    assert "content" in формы[0]["fields"]
    assert формы[0]["content_fields"] == ["text"]


def test_переписка_в_лог_не_попадает(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ ГЛАВНОЕ ОГРАНИЧЕНИЕ СТОРОЖА. В сообщении текст живого человека."""
    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    AvitoAdapter.normalize_history_message(
        сообщение(content={"text": "Мой телефон 89001112240, зовут Иван"}),
        chat=ЧАТ,
        account_user_id=111,
    )

    целиком = json.dumps(шпион.записи, ensure_ascii=False)
    assert "89001112240" not in целиком, "телефон клиента утёк в лог"
    assert "Иван" not in целиком, "имя клиента утекло в лог"
    assert "text" in целиком, "имя поля обязано остаться — ради него всё и заведено"


def test_незнакомое_поле_видно_сразу(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ РАДИ ЭТОГО СЛУЧАЯ СТОРОЖ И НУЖЕН.

    Если Авито однажды пришлёт цитату — под любым именем, — она появится в
    списке полей, и вопрос «умеет ли он цитирование» закроется фактом, а не
    рассуждением.
    """
    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    AvitoAdapter.normalize_history_message(
        сообщение(quote={"id": "am-0"}), chat=ЧАТ, account_user_id=111
    )

    формы = [з for з in шпион.записи if з["event"] == "avito.message_shape"]
    assert "quote" in формы[0]["fields"]


def test_список_обходится_целиком(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ РЕДКАЯ ФОРМА ЛЕЖИТ В СЕРЕДИНЕ НЕ РЕЖЕ, ЧЕМ В НАЧАЛЕ.

    Ручка отдаёт пачку сообщений. Посмотреть только на первое значило бы
    пропустить цитату, если ею ответили на третье сообщение из двадцати, — а
    ради неё всё и заведено.
    """
    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    мод._observe_message_shape(
        [сообщение(), сообщение(), сообщение(quote={"id": "am-0"})], откуда="v3.messages"
    )

    формы = [з for з in шпион.записи if з["event"] == "avito.message_shape"]
    поля = {tuple(ф["fields"]) for ф in формы}
    assert any("quote" in п for п in поля), "форма из середины пачки потеряна"


def test_одна_форма_пишется_однажды(monkeypatch: pytest.MonkeyPatch) -> None:
    """Загрузка истории — тысячи вызовов подряд; журнал заливать нельзя."""
    шпион = Шпион()
    monkeypatch.setattr(мод, "log", шпион)

    for i in range(200):
        AvitoAdapter.normalize_history_message(
            сообщение(id=f"am-{i}"), chat=ЧАТ, account_user_id=111
        )

    формы = [з for з in шпион.записи if з["event"] == "avito.message_shape"]
    assert len(формы) == 1, f"форма записана {len(формы)} раз вместо одного"


def test_сторож_не_роняет_разбор(monkeypatch: pytest.MonkeyPatch) -> None:
    """Наблюдение не имеет права стоить разобранного сообщения клиента."""

    class Взрывной(set):
        def __contains__(self, _x: object) -> bool:
            raise RuntimeError("что угодно")

    monkeypatch.setattr(мод, "_ФОРМЫ_СООБЩЕНИЙ", Взрывной())
    событие = AvitoAdapter.normalize_history_message(сообщение(), chat=ЧАТ, account_user_id=111)
    assert событие.text == "Здравствуйте", "упавший сторож съел сообщение клиента"
