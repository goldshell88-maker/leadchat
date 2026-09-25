"""Login brute-force guard (DESIGN §9, 01 §1.7).

ЧТО ЗАЩИЩАЕМ И ОТ ЧЕГО. Две разные угрозы, и меры у них разные:

* подбор пароля К ОДНОЙ учётке — ловится счётчиком по почте: 10 неудач в
  минуту, дальше ``403 account_locked``;
* перебор МНОГИХ учёток с одного адреса (password spraying) — по одной-две
  попытки на почту, счётчик по почте такое не увидит.

ПОЧЕМУ ПО АДРЕСУ НЕЛЬЗЯ СЧИТАТЬ СЫРЫЕ ПРОМАХИ. Весь офис выходит в интернет
с одного адреса. Считая промахи подряд, мы получаем: тринадцать человек, у
каждого утром бывает опечатка — десять опечаток за минуту, и не входит
НИКТО, включая тех, кто набирает пароль верно. Причём успешный вход такой
счётчик не сбрасывал, а каждая новая опечатка продлевала окно ещё на минуту:
офис мог не войти вовсе.

ЧТО СЧИТАЕМ ВМЕСТО ЭТОГО. Число РАЗНЫХ почт, по которым с адреса были
неудачи за окно. Это и есть подпись перебора: злоумышленник идёт по списку
учёток, офис — нет. Сотрудник ошибается в СВОЁМ пароле, и сколько бы раз он
ни ошибся, множество остаётся размером в одну почту. Порог ``LOGIN_SPRAY_
LIMIT`` взят с запасом над размером команды.

Окно СКОЛЬЗЯЩЕЕ: TTL продлевается на каждой неудаче, а не только на первой.
Это не косметика. С фиксированным окном блокировка на проде не срабатывала
вообще: nginx-зона `login` растягивает поток, счётчик доходил до предела к
54-й секунде и истекал на 60-й — 11-я попытка попадала уже в новое окно.
Проверено запросами к проду: 14 подряд неверных паролей, все 401, ни одного
403 account_locked (07 §4.1 A6).
"""

import hashlib

from redis.asyncio import Redis

LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW_SECONDS = 60

# Сколько РАЗНЫХ почт с одного адреса могут промахнуться за окно, прежде чем
# адрес считается перебирающим. Тридцать — с запасом больше команды (13
# операторов) и всё ещё несопоставимо мало для перебора по списку учёток.
# Порог именно такой, а не «13 + пара»: в офис приходят новые люди, и защита
# не должна ломаться от найма.
LOGIN_SPRAY_LIMIT = 30


def _email_digest(email: str) -> str:
    # Emails are PII — key by hash, not by raw address (05 §7.3 spirit).
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


def _email_key(email: str) -> str:
    return f"login_fail:email:{_email_digest(email)}"


def _ip_key(ip: str) -> str:
    """Множество почт, промахнувшихся с этого адреса.

    Именно множество, а не счётчик: см. модульный docstring — считать нужно
    разные учётки, а не количество промахов.
    """
    return f"login_fail:ip_emails:{ip}"


async def lockout_retry_after(redis: Redis, email: str, ip: str) -> int | None:
    """Seconds until the lock lifts, or None when login attempts are allowed."""
    value = await redis.get(_email_key(email))
    if value is not None and int(value) >= LOGIN_FAIL_LIMIT:
        ttl = await redis.ttl(_email_key(email))
        return max(int(ttl), 1)

    if await redis.scard(_ip_key(ip)) >= LOGIN_SPRAY_LIMIT:  # type: ignore[misc]
        ttl = await redis.ttl(_ip_key(ip))
        return max(int(ttl), 1)

    return None


async def record_login_failure(redis: Redis, email: str, ip: str) -> None:
    await redis.incr(_email_key(email))
    await redis.expire(_email_key(email), LOGIN_FAIL_WINDOW_SECONDS)

    await redis.sadd(_ip_key(ip), _email_digest(email))  # type: ignore[misc]
    await redis.expire(_ip_key(ip), LOGIN_FAIL_WINDOW_SECONDS)


async def reset_login_failures(redis: Redis, email: str, ip: str | None = None) -> None:
    """Удачный вход снимает подозрение — и с почты, и с адреса.

    Почту из множества адреса убираем обязательно: человек доказал, что знает
    пароль, и его утренняя опечатка не должна и дальше числиться уликой
    против всего офиса.
    """
    await redis.delete(_email_key(email))
    if ip is not None:
        await redis.srem(_ip_key(ip), _email_digest(email))  # type: ignore[misc]
