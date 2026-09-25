"""Refresh-кука сужена до SameSite=Strict (аудит безопасности 01.09).

⚠ ЧТО ЭТО МЕНЯЕТ, А ЧТО НЕТ. CSRF был закрыт и раньше: на межсайтовый POST
браузер не отправляет куку и при `lax`. Разница ровно одна — `lax` отдаёт её при
ПЕРЕХОДЕ ПО ССЫЛКЕ с чужого сайта, `strict` не отдаёт никогда.

Нашей куке первое не нужно вовсе: её читает единственный запрос — обновление
токена, и он всегда идёт из уже открытого приложения. То есть послабление мы
платили ни за что.

⚠ ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ ТЕСТОМ, А НЕ ГЛАЗАМИ. Флаги куки — одно слово в вызове,
и «починить» им что-нибудь при следующей беде проще всего: поставил `lax`, вход
заработал, а щель вернулась молча. SM-2 в smoke проверяет наличие флагов на
проде, но не их значения.
"""

import pytest

from tests.unit.conftest import DEFAULT_PASSWORD

pytestmark = pytest.mark.anyio


async def test_кука_обновления_строгая(client, users_by_role) -> None:
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "email": users_by_role["admin"].email,
            "password": DEFAULT_PASSWORD,
            "remember": True,
        },
    )
    assert r.status_code == 200, r.text
    куки = r.headers.get_list("set-cookie")
    строка = next((c for c in куки if c.lower().startswith("lc_refresh=")), None)
    assert строка is not None, "refresh-кука не выставлена вовсе"
    низ = строка.lower()
    assert "samesite=strict" in низ, (
        "refresh-кука ослаблена до lax — она снова поедет при переходе с чужого сайта"
    )
    assert "httponly" in низ, "кука стала видна скриптам — её украдёт первый же XSS"
    assert "secure" in низ, "кука поедет по http — её прочтут в открытой сети"
