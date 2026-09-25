"""Ссылка «подключите свой аккаунт Авито» не умирает от чужого запроса.

ЧТО БЫЛО. Переход по ссылке — публичный GET, и токен гасился ПЕРВОЙ СТРОКОЙ ручки,
до того как хоть что-то произошло. А ссылку по замыслу отправляют человеку в
мессенджер: «мы просто давали ссылку и всё» (требование заказчика, docstring
`issue_connect_link`). Значит первым по этому GET приходит не человек, а превью-бот
Telegram, сканер вложений почты или префетч браузера — и ссылка сгорает ДО нажатия.

ПОЧЕМУ ЭТО ДОРОГО ИМЕННО ЗДЕСЬ. На той стороне человек, у которого есть пароль от
аккаунта Авито и НЕТ доступа в LeadChat: перевыпустить ссылку он не может, это делает
администратор — который о случившемся не узнает. У заказчика девять аккаунтов Авито и
девять таких людей.

ЧТО СТАЛО. Гашение переехало в callback, вплотную к записи аккаунта: ссылка умирает от
СОСТОЯВШЕГОСЯ подключения, а не от чужого GET. Одноразовость сохранена — её держит тот
же GETDEL, просто в правильном месте.

⚠ ЧЕГО ЭТОТ ФАЙЛ НЕ ПРОВЕРЯЕТ. Полный путь до Авито (обмен кода, `get_self`) — он
требует внешней сети; тесты на него живут в test_avito_oauth.py. Здесь проверяется
ровно одно, но главное: переход по ссылке ССЫЛКУ НЕ ГАСИТ.
"""

from __future__ import annotations

import uuid

from app.services import avito_accounts as accounts_service


class TestConnectLinkSurvivesAStrangersRequest:
    async def test_following_the_link_does_not_burn_it(self, client, redis):
        """Первый GET не гасит: за превью-ботом должен успеть человек."""
        token, _ttl = await accounts_service.issue_connect_link(redis, actor_id=uuid.uuid4())

        # Так по ссылке ходит превью-бот мессенджера: обычный GET, без человека.
        r = await client.get(f"/api/v1/avito/connect/{token}", follow_redirects=False)
        assert r.status_code == 302, r.text
        assert "connect_link_expired" not in r.headers.get("location", "")

        # ⚠ ГЛАВНОЕ: ссылка ЖИВА — следом придёт человек и увидит согласие Авито.
        assert await accounts_service.peek_connect_link(redis, token) is not None, (
            "ссылка сгорела от чужого GET — человек, которому её послали, "
            "получит «недействительна» и перевыпустить не сможет"
        )

        # И повторный переход тоже ведёт на Авито, а не в тупик.
        r = await client.get(f"/api/v1/avito/connect/{token}", follow_redirects=False)
        assert r.status_code == 302
        assert "connect_link_expired" not in r.headers.get("location", "")

    async def test_the_token_travels_inside_the_state(self, redis):
        """Токен едет внутри `state` — иначе гасить его в callback'е будет нечем.

        Связь неочевидная и легко теряется при правке: `follow_connect_link` кладёт
        токен в state, а `avito_callback` достаёт его оттуда. Разорви эту пару — и
        ссылка станет бессрочной, то есть пересланная в общий чат будет работать
        вечно, ради чего одноразовость и вводили.
        """
        token, _ = await accounts_service.issue_connect_link(redis, actor_id=uuid.uuid4())
        state = await accounts_service.issue_oauth_state(
            redis, uuid.uuid4(), connect_link_token=token
        )
        raw = await redis.get(f"oauth_state:{state}")
        assert raw is not None
        import json

        assert json.loads(raw)["connect_link_token"] == token, (
            "токен ссылки не доехал до state — гасить в callback'е будет нечего"
        )

    async def test_consuming_is_still_one_time(self, redis):
        """Одноразовость на месте: второй `consume` уже ничего не находит."""
        token, _ = await accounts_service.issue_connect_link(redis, actor_id=uuid.uuid4())
        assert await accounts_service.consume_connect_link(redis, token) is not None
        assert await accounts_service.consume_connect_link(redis, token) is None
        assert await accounts_service.peek_connect_link(redis, token) is None
