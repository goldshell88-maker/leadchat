# --- сорвавшаяся загрузка истории (этап 1) ------------------------------------


async def test_a_crashed_backfill_stops_pretending_it_is_running(redis, make_avito_account):
    """Карточка канала перестаёт неделю врать «Загружаем историю…».

    ЧТО БЫЛО. Состояний было два: «нет ключа прогресса» и «есть». Второе
    называлось «идёт загрузка». Но ключ остаётся и когда задача упала — он
    живёт неделю, — и всё это время карточка показывала загрузку, которой
    давно нет. Человек ждал вместо того, чтобы нажать «Повторить».

    СМЕЩЕНИЕ ОТДАЁТСЯ И ПРИ СРЫВЕ: по нему видно, сколько успели, и что повтор
    продолжит, а не начнёт заново. «Сейчас всё задвоится» — первый страх при
    виде кнопки повтора.
    """
    from app.services import avito_accounts as svc

    account = await make_avito_account()
    await redis.set(f"backfill:{account.id}", 250)
    await redis.set(f"backfill:failed:{account.id}", "1")

    state = await svc.get_backfill_state(redis, account.id)

    assert state["status"] == "failed"
    assert state["chats_offset"] == 250


async def test_a_running_backfill_is_still_running(redis, make_avito_account):
    """Граница: без пометки о срыве всё как было."""
    from app.services import avito_accounts as svc

    account = await make_avito_account()
    await redis.set(f"backfill:{account.id}", 100)

    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "running"
    assert state["chats_offset"] == 100


async def test_no_key_means_nothing_is_running(redis, make_avito_account):
    from app.services import avito_accounts as svc

    account = await make_avito_account()
    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "idle"
    assert state["chats_offset"] is None


# --- ход загрузки на карточке канала (docs/41 §11) ---------------------------


async def test_the_card_gets_the_numbers_not_just_a_spinner(
    client, tokens, redis, make_avito_account
):
    """«Загружено N из M» обязано доехать до карточки канала.

    Полоса без чисел одинаково выглядит на первой минуте часового прогона и
    на последней, а вопрос к этому экрану ровно один — сколько осталось.
    Поэтому проверяется не форма ответа сервиса, а то, что числа проходят
    через схему ручки и не теряются по дороге.
    """
    import json

    from app.services import avito_accounts as svc

    account = await make_avito_account(880777)
    await redis.set(
        f"backfill:{account.id}",
        json.dumps(
            {
                "phase": "loading",
                "depth": svc.HISTORY_ALL,
                "total": 940,
                "loaded": 137,
                "queued": 4,
                "failed_chats": 2,
                "chats_offset": 139,
            }
        ),
    )

    resp = await client.get(
        "/api/v1/avito-accounts", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )

    assert resp.status_code == 200, resp.text
    card = next(i for i in resp.json()["items"] if i["id"] == str(account.id))
    assert card["backfill"] == {
        "status": "running",
        "phase": "loading",
        "depth": svc.HISTORY_ALL,
        "total": 940,
        "loaded": 137,
        "queued": 4,
        "failed_chats": 2,
        "chats_offset": 139,
    }
