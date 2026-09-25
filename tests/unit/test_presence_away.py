"""Статус «отошёл» и автораздача (#34).

ЧТО ЭТО ЗА СОСТОЯНИЕ. Диспетчер отходит от стола — обед, перекур, разговор по
телефону, разбор сложного случая с коллегой. Сказать об этом системе было
нечем: присутствие двоичное, и единственный способ выпасть из автораздачи —
закрыть приложение. То есть перестать видеть собственные диалоги и через три
минуты потерять розданные сторожу возврата.

Хуже того: отошедший обычно САМЫЙ СВОБОДНЫЙ по числу открытых диалогов, а
автораздача выбирает от самого свободного. Обращения доставались в первую
очередь тому, кого нет за столом, и ждали там, пока он вернётся.

ГРАНИЦА РЕШЕНА ЯВНО: «отошёл» — это «я за столом, но новых не берите».
Автораздача его пропускает; сторожа возврата считают присутствующим и диалоги
не отбирают. Обе стороны проверяются здесь, потому что вторая — это ровно то
место, где «отошёл» легко превратить в «уволился на полчаса».
"""

from __future__ import annotations

import uuid

import pytest

from app.ws import presence

pytestmark = pytest.mark.anyio


async def test_a_chosen_status_survives_a_reconnect(redis) -> None:
    """ГЛАВНАЯ ПРОВЕРКА ХРАНЕНИЯ: «отошёл» не слетает при переподключении.

    В `presence_connected` безусловно писалось "online", причём ВНЕ ветки «это
    первый сокет» — то есть при каждом новом соединении. А соединение
    переподключается постоянно: моргнула сеть, сервер разорвал его по таймауту
    кадров, человек открыл вторую вкладку. Статус, поставленный руками, слетал
    бы через минуту, и человек молча возвращался бы в автораздачу, не зная об
    этом.

    Статус, который сам себя сбрасывает, хуже отсутствующего: на него
    рассчитывают, а он не работает.
    """
    uid = uuid.uuid4()
    await presence.presence_connected(redis, uid, "conn-1")
    await presence.set_presence(redis, uid, presence.AWAY)

    # вторая вкладка / переподключение после обрыва
    await presence.presence_connected(redis, uid, "conn-2")

    status = await presence.presence_status_map(redis, [uid])
    assert status[uid] == presence.AWAY, "выбранное человеком состояние затёрто подключением"


async def test_heartbeat_keeps_the_status(redis) -> None:
    """Пинги идут каждые 25 секунд — они не должны «чинить» статус на online."""
    uid = uuid.uuid4()
    await presence.presence_connected(redis, uid, "c")
    await presence.set_presence(redis, uid, presence.AWAY)
    await presence.presence_heartbeat(redis, uid, "c")
    assert (await presence.presence_status_map(redis, [uid]))[uid] == presence.AWAY


async def test_leaving_clears_the_status_entirely(redis) -> None:
    """Закрыл приложение — нет ни «на месте», ни «отошёл», а просто нет в сети.

    Третьего значения в ключе не заводим намеренно: «ключа нет» и так означает
    ровно это, а два способа сказать одно и то же расходятся при первой правке.
    """
    uid = uuid.uuid4()
    await presence.presence_connected(redis, uid, "c")
    await presence.set_presence(redis, uid, presence.AWAY)
    await redis.delete(f"presence:{uid}")
    assert (await presence.presence_status_map(redis, [uid]))[uid] is None


async def test_unknown_status_is_refused(redis) -> None:
    """Мусор в ключ присутствия не попадает: его читают семь мест."""
    with pytest.raises(ValueError):
        await presence.set_presence(redis, uuid.uuid4(), "обедаю")


async def test_presence_map_still_answers_is_the_app_open(redis) -> None:
    """Старый ответ остался прежним, и на нём держатся сторожа.

    `presence_map` отвечает на вопрос «приложение открыто и отвечает на
    пинги», не различая «на месте» и «отошёл». Сторожа возврата спрашивают
    именно это: отошедший сидит за столом, и отбирать у него диалоги не за что.
    """
    uid = uuid.uuid4()
    await presence.presence_connected(redis, uid, "c")
    await presence.set_presence(redis, uid, presence.AWAY)
    assert (await presence.presence_map(redis, [uid]))[uid] is True


# --- ручка PUT /presence ------------------------------------------------------


async def test_anyone_may_set_their_own_state(client, tokens, redis) -> None:
    """Право — у всех, включая наблюдателя.

    Человек распоряжается СВОИМ состоянием. Спрашивать за это разрешение не у
    кого: чужое здесь и не задать — идентификатор берётся из сессии, а не из
    тела запроса.
    """
    for role in ("admin", "head", "manager", "observer"):
        r = await client.put(
            "/api/v1/presence",
            json={"status": "away"},
            headers={"Authorization": f"Bearer {tokens[role]}"},
        )
        assert r.status_code == 200, f"{role}: {r.text}"
        assert r.json()["status"] == "away"


async def test_anonymous_is_refused(client) -> None:
    r = await client.put("/api/v1/presence", json={"status": "away"})
    assert r.status_code == 401


async def test_offline_cannot_be_declared_by_hand(client, tokens) -> None:
    """«Не в сети» — не выбор человека, а факт отсутствия соединения.

    Разрешить его значило бы позволить объявить себя отсутствующим, продолжая
    держать открытые диалоги, — состояние, которого сторожа не ждут: они
    смотрят на живой сокет, а он был бы жив.
    """
    r = await client.put(
        "/api/v1/presence",
        json={"status": "offline"},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "bad_status"
