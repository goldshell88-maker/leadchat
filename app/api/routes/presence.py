"""Своё состояние: «на месте» или «отошёл» (01 §11.6).

«Отошёл» — «новых обращений не раздавайте»: автораздача такого сотрудника
пропускает, коллеги видят его в списках с пометкой.

Начатые диалоги при этом не защищены сами по себе: если в «Распределении»
включено освобождение и человека нет в исключениях, через 15 минут в «Отошёл»
(или не в сети) его диалоги освобождает ``reclaim.release_unavailable``.
Что именно ждёт человека, экран спрашивает у ``GET /presence/release``.

Хранится в том же ключе, что и присутствие, с тем же временем жизни: статус
имеет смысл, только пока приложение открыто.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_redis
from app.core.errors import ApiError
from app.models import User
from app.services import app_settings
from app.ws import presence

router = APIRouter()


class PresenceIn(BaseModel):
    status: str
    #: «отошёл» поставила не рука человека, а авто-«отошёл» вкладки, в которой
    #: 30 минут не было касаний (`автоОтошёл.ts`). Такой статус — догадка одной
    #: вкладки о человеке, и сервер её проверяет по активности на всех
    #: устройствах. Ручной переключатель флага не шлёт и принимается всегда.
    auto: bool = False


@router.get("/presence")
async def get_my_presence(
    user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Своё состояние — чтобы экран не спорил с сервером после перезагрузки.

    ⚠ ЗАЧЕМ ЭТО ЗАВЕДЕНО (28.08). Ручки чтения не было вовсе, и хранилище на
    клиенте стартовало со значения «на месте» — «состояние только что открытого
    приложения». Но статус переживает перезагрузку страницы: он лежит в том же
    ключе, что и присутствие, а тот живёт минутами, и `presence_connected`
    при переподключении текущее значение СОХРАНЯЕТ.

    Получалось так: диспетчер поставил «отошёл», нажал F5 (или приложение
    перезапустилось само) — и весь интерфейс сообщает «На месте». Автораздача
    при этом продолжает его пропускать, потому что на сервере он по-прежнему
    «отошёл». Человек сидит и ждёт обращений, которых ему не дадут, и ни одна
    строчка на экране не говорит, почему.

    «Не в сети» отсюда не возвращается: у только что открытого приложения
    соединение уже есть. Пустой ключ означает «состояние не выбирали» — это и
    есть «на месте».
    """
    статусы = await presence.presence_status_map(redis, [user.id])
    return {"status": статусы.get(user.id) or presence.ONLINE}


@router.put("/presence")
async def set_my_presence(
    payload: PresenceIn,
    user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Поставить себе «на месте» / «отошёл».

    Права не проверяются отдельно: человек распоряжается своим состоянием, а
    не чужим. Чужое здесь и не задать — идентификатор берётся из сессии, а не
    из тела запроса.

    «Не в сети» через эту ручку не ставится: это не выбор человека, а факт
    отсутствия соединения. Разрешить его значило бы позволить объявить себя
    отсутствующим, продолжая держать открытые диалоги, — состояние, которого
    сторожа не ждут.
    """
    if payload.status not in presence.PRESENCE_STATUSES:
        raise ApiError(
            "unprocessable",
            "Состояние может быть «на месте» или «отошёл»",
            status=422,
            details={"reason": "bad_status", "allowed": list(presence.PRESENCE_STATUSES)},
        )
    if payload.status == presence.ONLINE:
        # «На месте» с любого устройства — человека видели активным.
        await presence.mark_active(redis, user.id)
    elif payload.auto and await presence.recently_active(redis, user.id):
        # ПРОСТАИВАЮЩАЯ ВКЛАДКА НЕ ПЕРЕБИВАЕТ РАБОТАЮЩЕЕ УСТРОЙСТВО (владелец
        # 19.09: работал с телефона, компьютер каждые две минуты слал «отошёл»,
        # сторож 15 минут «недоступен» отбирал диалоги у человека в сети).
        # Память об активности у вкладки своя на браузер, у сервера — на
        # человека; побеждает сервер. Вкладке отвечаем текущим статусом: она
        # запомнит его как отправленный и не будет спорить.
        статусы = await presence.presence_status_map(redis, [user.id])
        return {"status": статусы.get(user.id) or presence.ONLINE}
    await presence.set_presence(redis, user.id, payload.status)
    return {"status": payload.status}


@router.get("/presence/release")
async def my_release_rule(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int | None]:
    """Освободятся ли мои диалоги, если я отойду, и через сколько минут.

    ``None`` — не освободятся: правило выключено или я в списке исключений.
    Подписи к «Отошёл» строятся по этому ответу, а не по общему обещанию.
    """
    from app.scheduler.jobs.reclaim import UNAVAILABLE_AFTER_MINUTES

    enabled = await app_settings.get(db, app_settings.RELEASE_UNAVAILABLE_ENABLED)
    exempt = app_settings.parse_exempt_ids(
        await app_settings.get(db, app_settings.RELEASE_UNAVAILABLE_EXEMPT)
    )
    applies = bool(enabled) and user.id not in exempt
    return {"release_after_minutes": UNAVAILABLE_AFTER_MINUTES if applies else None}
