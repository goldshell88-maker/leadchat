"""Кто подписан на события аккаунта Авито (#39).

ЗАЧЕМ ЭТО ВООБЩЕ. На аккаунтах заказчика работает JivoChat, и главный
неотвеченный вопрос переезда — что случится с её подпиской, когда подпишемся
мы: подписки сосуществуют, наша перебивает чужую или чужая перебивает нашу.
От ответа зависит, можно ли вести пилот на двух-трёх диспетчерах параллельно с
работающим Jivo — или переезд обязан быть одномоментным для всей смены.

ПОЧЕМУ ЭТО НЕЛЬЗЯ БЫЛО ПРОСТО ПОПРОБОВАТЬ. Подключение аккаунта в интерфейсе
регистрирует наш вебхук сразу и без вопросов. Подключить боевой аккаунт
«посмотреть, что будет» невозможно: это уже опыт над работающим Jivo, и если
подписки не сосуществуют, смена останется без входящих.

Метод только читает — им и можно узнать ответ, ничего не сломав.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import settings
from app.integrations.avito.client import AvitoClient
from app.integrations.avito.errors import AvitoApiError

pytestmark = pytest.mark.anyio

URL = f"{settings.avito_api_base}/messenger/v1/subscriptions"
JIVO = {"url": "https://jivo.example/avito/hook", "version": "3.0.0"}
OURS = {"url": "https://chat.partner-lead-centre.ru/api/hooks/avito/x?secret=s", "version": "3.0.0"}


@respx.mock
async def test_the_spec_verb_is_tried_first() -> None:
    """POST — как в спецификации, а не GET из нашего пересказа.

    В каталоге методов этот эндпоинт записан ДВАЖДЫ и по-разному: в разделе
    «чего мы не используем» как GET, в таблице из спецификации Авито как POST.
    Имитатор реализовал GET — то есть подтвердил нашу же догадку. Ровно так
    этот проект уже дважды попал на боевом Авито: путь v2 вместо v3 и 403
    вместо 401. Поэтому первым пробуется то, что взято из спецификации.
    """
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"subscriptions": [JIVO]}))
    items = await AvitoClient().list_subscriptions("tok")
    assert route.called
    assert items == [JIVO]


@respx.mock
async def test_it_falls_back_to_the_other_verb() -> None:
    """Если спецификация врёт, второй запрос выручает — и он один.

    Угадывать глагол нельзя, но и хоронить возможность из-за расхождения в
    собственном документе — тоже. Лишний запрос случается один раз и только
    когда первый ответил «нет такого метода».
    """
    respx.post(URL).mock(return_value=httpx.Response(405))
    get_route = respx.get(URL).mock(
        return_value=httpx.Response(200, json={"subscriptions": [JIVO, OURS]})
    )
    items = await AvitoClient().list_subscriptions("tok")
    assert get_route.called
    assert len(items) == 2


@respx.mock
async def test_a_reply_without_the_list_is_an_error() -> None:
    """Молча вернуть пустоту нельзя: «подписок нет» и «мы не поняли ответ» —
    разные новости, и вторая означала бы, что вопрос остался без ответа, а
    человек решил, что ответ получен."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    with pytest.raises(AvitoApiError):
        await AvitoClient().list_subscriptions("tok")


@respx.mock
async def test_an_empty_list_is_a_legitimate_answer() -> None:
    """Пусто — это ответ «никто не подписан», а не сбой."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"subscriptions": []}))
    assert await AvitoClient().list_subscriptions("tok") == []


async def test_only_the_owner_may_look(client, tokens, seed_conversation) -> None:
    """Право — `accounts:manage`, то есть только администратор.

    Ответ показывает адреса вебхуков сторонних систем; это сведения о том, как
    устроена работа компании, и раздавать их всей смене не за чем.
    """
    for role in ("head", "manager", "observer"):
        r = await client.get(
            f"/api/v1/avito-accounts/{seed_conversation.account.id}/subscriptions",
            headers={"Authorization": f"Bearer {tokens[role]}"},
        )
        assert r.status_code == 403, f"{role}: {r.text}"


async def test_anonymous_is_refused(client, seed_conversation) -> None:
    r = await client.get(f"/api/v1/avito-accounts/{seed_conversation.account.id}/subscriptions")
    assert r.status_code == 401
